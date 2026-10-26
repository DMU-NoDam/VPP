"""
LightGBM 학습 스크립트 (오프라인 실행 전용)

forecast_api가 요청마다 재학습하지 않도록, 이 스크립트는 따로 실행해서
모델을 saved/ 폴더에 파일로 저장해둔다. API는 이 저장된 파일을 불러와
예측만 수행한다.

시간순으로 3구간을 나눈다 (랜덤 분할 금지):

    |------------- train -------------|---- calib (4주) ----|-- test (2주) --|
     모델 학습                          PI margin 계산          최종 평가만

- calib: 여러 시점에서 실제로 168시간 이어서 예측해보고, "h시간 뒤 예측" 오차로
         h번째 PI margin을 정한다 (목표 90%).
- test : 학습/margin 계산 어디에도 안 쓴 구간. 24h·168h 이어서 예측한 성적과
         PI 커버리지를 여기서만 잰다. (test를 보고 margin을 조정하지 않는다)

저장 파일:
    saved/{target}_lgbm.txt : 모델
    saved/{target}_pi.json  : 시간대별 margin(168개) + test 성적

실행:
    python3 models/train_lgbm.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[1]))  # forecast_api/ 를 import 경로에 추가

from data_client import available_range, get_mock_data, weather_columns
from features import build_features
from metrics import PRIMARY_METRIC, mape, nmae
from pi import apply_horizon_pi, compute_horizon_margins

SAVED_DIR = Path(__file__).resolve().parent / "saved"
SAVED_DIR.mkdir(exist_ok=True)

TARGET_COLUMN = {"demand": "demand_mw", "solar": "solar_mw", "wind": "wind_mw"}


def _feature_columns(target: str) -> list[str]:
    """시간 피처 + 기상(지점별 + 평균, solar/wind만) + lag/rolling 피처."""
    col = TARGET_COLUMN[target]
    return (["hour", "dayofweek", "is_weekend"]
            + weather_columns(target)
            + [f"{col}_lag_1h", f"{col}_lag_24h", f"{col}_lag_168h", f"{col}_roll_mean_24h"])


# feature 컬럼 정의: target별로 쓸 수 있는 컬럼이 조금씩 다르다
# (solar: 일사량 7개 지점 + 평균 / wind: 풍속 6개 지점 + 평균 — data_client.weather_columns)
FEATURE_COLUMNS = {t: _feature_columns(t) for t in TARGET_COLUMN}

# target별로 어떤 평가지표를 쓸지 (스펙 기준: 수요=MAPE, 재생에너지=nMAE)
METRIC_FN = {"demand": mape, "solar": nmae, "wind": nmae}
METRIC_NAME = {"demand": "MAPE", "solar": "nMAE", "wind": "nMAE"}

TEST_DAYS = 14
CALIB_DAYS = 28
MAX_HORIZON = 168
CALIB_ORIGIN_STEP_H = 4   # calib 구간에서 4시간마다 예측 시작 (표본 수 확보)
CONFIDENCE = 0.9


def _fit(X: pd.DataFrame, y: pd.Series) -> lgb.Booster:
    model = lgb.LGBMRegressor(n_estimators=300, learning_rate=0.05, num_leaves=31, verbosity=-1)
    model.fit(X, y)
    return model.booster_


def _horizon_report(target: str, pred: np.ndarray, actual: np.ndarray,
                    margins: np.ndarray) -> dict[str, float]:
    """(n_origins, H) 예측/실제 → 시험 구간 성적 dict (API metrics에 그대로 들어감)."""
    per_origin_mape = [mape(a, p) for a, p in zip(actual, pred)]
    per_origin_nmae = [nmae(a, p) for a, p in zip(actual, pred)]
    lower, upper = zip(*(apply_horizon_pi(p, margins) for p in pred))
    lower, upper = np.array(lower), np.array(upper)
    inside = (actual >= lower) & (actual <= upper)
    result = {
        "mape": float(np.nanmean(per_origin_mape)),
        "nmae": float(np.nanmean(per_origin_nmae)),
        "coverage": float(np.mean(inside) * 100),
        "interval_width_mean": float(np.mean(upper - lower)),
        "n_origins": float(len(pred)),
    }
    result["primary_value"] = result[PRIMARY_METRIC[target]]
    return result


def train_one(target: str, start: str | None = None, end: str | None = None) -> dict:
    # 기간을 안 주면 실제 CSV에 들어있는 전체 기간으로 학습
    if start is None or end is None:
        start, end = available_range(target)

    from forecast_engine import recursive_backtest  # 순환 import 방지 (forecast_engine이 이 파일을 import함)

    print("=" * 60)
    print(f"target = {target}  ({start} ~ {end})")
    print("=" * 60)

    target_col = TARGET_COLUMN[target]
    feature_cols = FEATURE_COLUMNS[target]

    # 1) 데이터 가져오기 + feature 손질
    raw = get_mock_data(target, start=start, end=end).sort_values("datetime").reset_index(drop=True)
    df = build_features(raw, target_col)

    # 2) train / calib / test 시간순 분할
    data_end = raw["datetime"].max()
    test_start = (data_end - pd.Timedelta(days=TEST_DAYS)).ceil("D")
    calib_start = test_start - pd.Timedelta(days=CALIB_DAYS)
    train = df[df["datetime"] < calib_start]
    test = df[df["datetime"] >= test_start]
    print(f"train ~{calib_start.date()} ({len(train)}행) / calib {CALIB_DAYS}일 / "
          f"test {test_start.date()}~ ({len(test)}행)")

    # 3) 1차 학습 (train만) — calib 구간 오차를 재기 위한 모델
    calib_model = _fit(train[feature_cols], train[target_col])

    # 4) calib 구간에서 168시간 이어서 예측 → 시간대별 margin
    calib_origins = list(pd.date_range(calib_start, test_start - pd.Timedelta(hours=MAX_HORIZON),
                                       freq=f"{CALIB_ORIGIN_STEP_H}h"))
    cb = recursive_backtest(target, calib_model, raw, calib_origins, MAX_HORIZON)
    margins = compute_horizon_margins(np.abs(cb["actual"] - cb["pred"]), CONFIDENCE)
    print(f"PI margin (calib {len(calib_origins)}개 시점): "
          f"1h {margins[0]:.1f} / 24h {margins[23]:.1f} / 168h {margins[-1]:.1f}")

    # 5) 2차 학습 (train + calib) — 최근 4주까지 배운 최종 모델. test는 여전히 안 씀.
    #    margin은 1차 모델 오차로 정한 값을 그대로 쓴다. 최종 모델이 더 정확하면
    #    구간이 약간 넓은(보수적인) 쪽으로 틀리게 된다.
    train_full = df[df["datetime"] < test_start]
    booster = _fit(train_full[feature_cols], train_full[target_col])
    print(f"최종 모델 재학습: train+calib {len(train_full)}행")

    # (참고) 1시간 앞 예측 성적 — 실제 과거값을 넣어주므로 낙관적인 수치
    one_step = METRIC_FN[target](test[target_col].values, booster.predict(test[feature_cols]))
    print(f"[참고] 1시간 앞 예측 {METRIC_NAME[target]}: {one_step:.2f}%")

    # 6) test 구간 (최종 모델로 평가): 매일 0시에 예측 시작 → 24h / 168h 성적 + PI 커버리지
    test_metrics = {}
    for H in (24, 168):
        origins = list(pd.date_range(test_start, data_end - pd.Timedelta(hours=H) + pd.Timedelta(hours=1),
                                     freq="D"))
        tb = recursive_backtest(target, booster, raw, origins, H)
        rep = _horizon_report(target, tb["pred"], tb["actual"], margins[:H])
        test_metrics[f"{H}h"] = rep
        print(f"[test {H:3d}h] {METRIC_NAME[target]} {rep['primary_value']:.2f}% / "
              f"PI 커버리지 {rep['coverage']:.1f}% / 평균 구간폭 {rep['interval_width_mean']:.1f} MW "
              f"(시점 {len(origins)}개)")

    # 7) 저장 (API가 나중에 이 파일들을 불러와서 씀)
    booster.save_model(str(SAVED_DIR / f"{target}_lgbm.txt"))
    pi_info = {
        "method": "conformal_horizon",
        "confidence": CONFIDENCE,
        "train_period": [str(raw["datetime"].min()), str(test_start)],
        "calib_period": [str(calib_start), str(test_start)],
        "calib_origins": len(calib_origins),
        "test_period": [str(test_start), str(data_end)],
        "margins": [round(float(m), 3) for m in margins],
        "test_metrics": test_metrics,
    }
    (SAVED_DIR / f"{target}_pi.json").write_text(
        json.dumps(pi_info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"모델/PI 저장 완료: saved/{target}_lgbm.txt, saved/{target}_pi.json\n")
    return test_metrics


if __name__ == "__main__":
    summary = {t: train_one(t) for t in ["demand", "solar", "wind"]}

    print("=" * 60)
    print("LightGBM 최종 결과 (test 구간, 이어서 예측)")
    print("=" * 60)
    for t, m in summary.items():
        for h in ("24h", "168h"):
            print(f"{t:6s} {h:>4s}  {METRIC_NAME[t]} {m[h]['primary_value']:6.2f}%  "
                  f"PI 커버리지 {m[h]['coverage']:5.1f}%")
