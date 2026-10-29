"""
LightGBM 학습 스크립트 (오프라인 실행 전용)

forecast_api가 요청마다 재학습하지 않도록, 이 스크립트는 따로 실행해서
모델을 saved/ 폴더에 파일로 저장해둔다. API는 이 저장된 파일을 불러와
예측만 수행한다.

시간순으로 3구간을 나눈다 (랜덤 분할 금지):

    |------------- train -------------|---- calib (4주) ----|-- test (2주) --|
     1차 학습                            PI margin 계산          최종 평가만
     |----------- 2차 학습 (train + calib) -------------------|

- calib: 1차 모델(train만 학습)로 API와 같은 방식으로 예측해보고, 그 오차로 90% PI margin을 정한다.
    수요(recursive)  : "몇 시간 뒤" 별 margin 168개
    태양광/풍력(direct): "몇 시" 별 margin 24개
- 2차 학습: margin을 정한 뒤 train+calib 전체로 다시 학습해서 저장 (최근 4주까지 반영)
- test : 학습/margin 계산 어디에도 안 쓴 구간. 24h·168h 성적과 PI 커버리지를 여기서만 잰다.

저장 파일:
    saved/{target}_lgbm.txt : 모델
    saved/{target}_pi.json  : PI margin + test 성적

실행:
    python3 models/train_lgbm.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[1]))  # forecast_api/ 를 import 경로에 추가

from data_client import available_range, get_mock_data
from features import TARGET_COLUMN, build_features, feature_columns
from forecast_engine import STRATEGY, TARGET_MODE, backtest, interval
from metrics import PRIMARY_METRIC, SPEC_THRESHOLDS, mape, nmae
from pi import compute_horizon_margins, compute_hourly_margins

SAVED_DIR = Path(__file__).resolve().parent / "saved"
SAVED_DIR.mkdir(exist_ok=True)

FEATURE_COLUMNS = {t: feature_columns(t) for t in TARGET_COLUMN}  # 하위 호환
METRIC_NAME = {"demand": "MAPE", "solar": "nMAE", "wind": "nMAE"}

# target별 LightGBM 하이퍼파라미터. 풍력은 데이터 잡음이 커서 과적합을 막도록 더 단순하게.
LGBM_PARAMS = {
    "demand": {"n_estimators": 300, "learning_rate": 0.05, "num_leaves": 31},
    "solar": {"n_estimators": 400, "learning_rate": 0.05, "num_leaves": 31},
    "wind": {"n_estimators": 300, "learning_rate": 0.05, "num_leaves": 15, "min_child_samples": 80},
}

TEST_DAYS = 14
CALIB_DAYS = 28
MAX_HORIZON = 168
CALIB_ORIGIN_STEP_H = 4   # (수요) calib 구간에서 4시간마다 예측 시작 (표본 수 확보)
CONFIDENCE = 0.9


def _fit(target: str, df: pd.DataFrame) -> lgb.Booster:
    x = df[feature_columns(target)].to_numpy(dtype=float)
    y = df[TARGET_COLUMN[target]].to_numpy(dtype=float)
    if TARGET_MODE[target] == "cf":
        y = y / df["capacity_mw"].to_numpy(dtype=float)  # 이용률로 학습
    model = lgb.LGBMRegressor(verbosity=-1, **LGBM_PARAMS[target])
    model.fit(x, y)
    return model.booster_


def _calibrate(target: str, model: lgb.Booster, raw: pd.DataFrame,
               calib_start: pd.Timestamp, test_start: pd.Timestamp) -> dict:
    """1차 모델로 calib 구간을 API와 같은 방식으로 예측 → PI margin."""
    if STRATEGY[target] == "recursive":
        origins = list(pd.date_range(calib_start, test_start - pd.Timedelta(hours=MAX_HORIZON),
                                     freq=f"{CALIB_ORIGIN_STEP_H}h"))
        cb = backtest(target, model, raw, origins, MAX_HORIZON)
        margins = compute_horizon_margins(np.abs(cb["actual"] - cb["pred"]), CONFIDENCE)
        return {"pi_type": "horizon", "margins": margins, "calib_origins": len(origins)}

    # direct: 예측이 시작 시점과 무관하므로, 매일 0시에 24시간씩 이어붙여 calib 전체를 덮는다
    origins = list(pd.date_range(calib_start, test_start - pd.Timedelta(hours=24), freq="D"))
    cb = backtest(target, model, raw, origins, 24)
    margins = compute_hourly_margins(np.abs(cb["actual"] - cb["pred"]).ravel(),
                                     cb["hour"].ravel(), CONFIDENCE)
    return {"pi_type": "hourly", "margins": margins, "calib_origins": len(origins)}


def _report(target: str, bt: dict, pi_info: dict, datetimes: list[pd.DatetimeIndex]) -> dict:
    """(n_origins, H) 예측/실제 → 시험 구간 성적 dict (API metrics에 그대로 들어감)."""
    pred, actual = bt["pred"], bt["actual"]
    cap = bt.get("capacity")
    lower, upper = zip(*(interval(pi_info, p, dts) for p, dts in zip(pred, datetimes)))
    lower, upper = np.array(lower), np.array(upper)
    inside = (actual >= lower) & (actual <= upper)
    result = {
        "mape": float(np.nanmean([mape(a, p) for a, p in zip(actual, pred)])),
        "nmae": float(np.nanmean([nmae(a, p, c if cap is not None else None)
                                  for a, p, c in zip(actual, pred, cap if cap is not None
                                                     else [None] * len(pred))])),
        "nmae_mean": float(np.nanmean([nmae(a, p) for a, p in zip(actual, pred)])),
        "coverage": float(np.mean(inside) * 100),
        "interval_width_mean": float(np.mean(upper - lower)),
        "n_origins": float(len(pred)),
    }
    result["primary_value"] = result[PRIMARY_METRIC[target]]
    return result


def _passes(target: str, horizon: str, m: dict) -> bool:
    if target == "demand":
        limit = SPEC_THRESHOLDS["demand_mape_24h" if horizon == "24h" else "demand_mape_168h"]
    else:
        limit = SPEC_THRESHOLDS[f"{target}_nmae"]
    return m["primary_value"] <= limit and m["coverage"] >= SPEC_THRESHOLDS["pi_coverage"]


def train_one(target: str, start: str | None = None, end: str | None = None) -> dict:
    # 기간을 안 주면 실제 CSV에 들어있는 전체 기간으로 학습
    if start is None or end is None:
        start, end = available_range(target)

    print("=" * 60)
    print(f"target = {target}  ({start} ~ {end})  "
          f"방식: {STRATEGY[target]} / 정답: {TARGET_MODE[target]}")
    print("=" * 60)

    raw = get_mock_data(target, start=start, end=end).sort_values("datetime").reset_index(drop=True)
    df = build_features(raw, TARGET_COLUMN[target])

    # train / calib / test 시간순 분할
    data_end = raw["datetime"].max()
    test_start = (data_end - pd.Timedelta(days=TEST_DAYS)).ceil("D")
    calib_start = test_start - pd.Timedelta(days=CALIB_DAYS)
    train = df[df["datetime"] < calib_start]
    print(f"train ~{calib_start.date()} ({len(train)}행) / calib {CALIB_DAYS}일 / "
          f"test {test_start.date()} ~ {data_end}")

    # 1차 학습(train) → calib 오차로 PI margin
    calib_model = _fit(target, train)
    cal = _calibrate(target, calib_model, raw, calib_start, test_start)
    m = cal["margins"]
    if cal["pi_type"] == "horizon":
        print(f"PI margin(몇 시간 뒤): 1h {m[0]:.0f} / 24h {m[23]:.0f} / 168h {m[-1]:.0f} MW")
    else:
        print(f"PI margin(몇 시): 0시 {m[0]:.0f} / 6시 {m[6]:.0f} / 12시 {m[12]:.0f} / "
              f"18시 {m[18]:.0f} MW")

    # 2차 학습(train + calib) → 최종 모델
    t0 = time.time()
    booster = _fit(target, df[df["datetime"] < test_start])
    train_seconds = time.time() - t0

    # test: 매일 0시에 예측 시작 → 24h / 168h 성적 + PI 커버리지
    pi_info = {"pi_type": cal["pi_type"], "margins": m}
    test_metrics = {}
    for hz in (24, 168):
        last_origin = data_end - pd.Timedelta(hours=hz) + pd.Timedelta(hours=1)
        origins = list(pd.date_range(test_start, last_origin, freq="D"))
        tb = backtest(target, booster, raw, origins, hz)
        dts = [pd.date_range(o, periods=hz, freq="h") for o in origins]
        rep = _report(target, tb, pi_info, dts)
        test_metrics[f"{hz}h"] = rep
        ok = "통과" if _passes(target, f"{hz}h", rep) else "미달"
        extra = f" (실제평균 기준 {rep['nmae_mean']:.1f}%)" if target != "demand" else ""
        print(f"[test {hz:3d}h] {METRIC_NAME[target]} {rep['primary_value']:.2f}%{extra} / "
              f"PI 커버리지 {rep['coverage']:.1f}% / "
              f"평균 구간폭 {rep['interval_width_mean']:.0f} MW "
              f"(시점 {len(origins)}개) → {ok}")

    # 저장 (API가 이 파일들을 불러와서 씀)
    booster.save_model(str(SAVED_DIR / f"{target}_lgbm.txt"))
    out = {
        "method": "conformal",
        "pi_type": cal["pi_type"],
        "strategy": STRATEGY[target],
        "target_mode": TARGET_MODE[target],
        "confidence": CONFIDENCE,
        "train_seconds": round(train_seconds, 1),
        "train_period": [str(raw["datetime"].min()), str(test_start)],
        "calib_period": [str(calib_start), str(test_start)],
        "calib_origins": cal["calib_origins"],
        "test_period": [str(test_start), str(data_end)],
        "margins": [round(float(x), 3) for x in m],
        "test_metrics": test_metrics,
    }
    (SAVED_DIR / f"{target}_pi.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"모델/PI 저장 완료: saved/{target}_lgbm.txt, saved/{target}_pi.json\n")
    return test_metrics


if __name__ == "__main__":
    summary = {t: train_one(t) for t in ["demand", "solar", "wind"]}

    print("=" * 60)
    print("LightGBM 최종 결과 (test 구간, API와 같은 방식으로 예측)")
    print("=" * 60)
    for t, ms in summary.items():
        for h in ("24h", "168h"):
            m = ms[h]
            print(f"{t:6s} {h:>4s}  {METRIC_NAME[t]} {m['primary_value']:6.2f}%  "
                  f"PI 커버리지 {m['coverage']:5.1f}%  → {'통과' if _passes(t, h, m) else '미달'}")
