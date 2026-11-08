"""
예측 엔진 — data_client / features / models / metrics / pi 를 조립해서
"미래 horizon_hours 시간의 예측값 + 90% 구간"을 만들어내는 핵심 로직.

router.py는 이 모듈을 호출하기만 하고, HTTP 관련 코드는 여기 두지 않는다.

예측 방식 (target별):
- demand: recursive — 1시간씩 예측하고, 그 예측값을 다음 시간의 lag로 다시 넣는다.
          피처 = 과거 수요(lag) + 기온·냉난방도일 + 달력(공휴일 포함)
- solar : direct — 기상 + 달력만으로 각 시각을 바로 예측. 정답은 "이용률(발전량/설비용량)"로
          학습하고, 마지막에 설비용량을 곱한다 (태양광 설비가 계속 늘어나는 걸 반영).
- wind  : direct — 기상 + 달력만으로 각 시각의 발전량(MW)을 바로 예측.

미래 기상(기온/일사량/풍속)은 원래 기상청 "예보"를 써야 하지만, 예보 API가 아직 없어서
data_client.get_weather로 해당 시각의 "관측값"을 예보값 대용으로 쓴다.
관측값도 없는 시각(데이터 끝 이후)은 과거 데이터의 "시간대별 평균"으로 채운다.
"""

from __future__ import annotations

import json
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from data_client import get_mock_data, get_weather, weather_columns
from features import TARGET_COLUMN, build_exog_features, build_features, feature_columns
from metrics import evaluate
from pi import apply_horizon_pi, apply_hourly_pi, fit_conformal

SAVED_DIR = Path(__file__).resolve().parent / "models" / "saved"

STRATEGY = {"demand": "recursive", "solar": "direct", "wind": "direct"}
TARGET_MODE = {"demand": "mw", "solar": "cf", "wind": "mw"}  # cf = 이용률(발전량/설비용량)

# 미래 구간 기상 피처(가운데 정렬 이동평균)를 계산할 때 앞뒤로 붙이는 여유 시간
_CONTEXT_BEFORE_H = 24
_CONTEXT_AFTER_H = 12

# 하위 호환: 예전 코드에서 쓰던 이름
FEATURE_COLUMNS = {t: feature_columns(t) for t in TARGET_COLUMN}


def _load_model(target: str, model_name: str) -> tuple[lgb.Booster, str]:
    """model_name이 "auto"면 lightgbm을 쓴다 (비교 결과 LightGBM이 더 정확했음)."""
    if model_name == "lstm":
        lstm_path = SAVED_DIR / f"{target}_lstm.pt"
        if not lstm_path.exists():
            raise FileNotFoundError(
                f"{lstm_path} 없음 — 본인 컴퓨터에서 models/train_lstm.py 먼저 실행 필요"
            )
        raise NotImplementedError("LSTM 서빙 로직은 아직 미구현 (LightGBM 사용 권장)")

    lgbm_path = SAVED_DIR / f"{target}_lgbm.txt"
    if not lgbm_path.exists():
        raise FileNotFoundError(f"{lgbm_path} 없음 — models/train_lgbm.py 먼저 실행 필요")
    return lgb.Booster(model_file=str(lgbm_path)), "lightgbm"


def _future_exog(target: str, history: pd.DataFrame, horizon_hours: int) -> pd.DataFrame:
    """미래 horizon_hours 시간의 달력·기상 피처 표 (lag 제외).

    기상값 우선순위: (1) 그 시각의 관측값(예보 대용) → (2) history에 있는 값
                    → (3) history의 시간대별 평균
    """
    cols = weather_columns(target)
    future_start = history["datetime"].max() + pd.Timedelta(hours=1)
    future_idx = pd.date_range(future_start, periods=horizon_hours, freq="h")
    ctx_idx = pd.date_range(future_start - pd.Timedelta(hours=_CONTEXT_BEFORE_H),
                            future_idx[-1] + pd.Timedelta(hours=_CONTEXT_AFTER_H), freq="h")

    observed = get_weather(target, start=str(ctx_idx[0]), end=str(ctx_idx[-1]))
    frame = observed.reindex(ctx_idx)
    past = history.set_index("datetime")[cols]
    frame = frame.fillna(past.reindex(ctx_idx))
    profile = history.groupby(history["datetime"].dt.hour)[cols].mean()
    for c in cols:
        missing = frame[c].isna()
        frame.loc[missing, c] = profile[c].reindex(frame.index[missing].hour).to_numpy()
    frame = frame.fillna(0.0)

    frame = frame.rename_axis("datetime").reset_index()
    exog = build_exog_features(frame, target)
    return exog[exog["datetime"].isin(future_idx)].reset_index(drop=True)


def _recursive_forecast(target: str, model, history: pd.DataFrame,
                        horizon_hours: int) -> pd.DataFrame:
    """1시간씩 순차적으로 미래를 예측하면서, 예측값을 다음 스텝의 lag로 재사용한다.
    history: 과거 168시간 이상 (datetime, 정답 컬럼, 기상 컬럼)"""
    target_col = TARGET_COLUMN[target]
    feature_cols = feature_columns(target)
    exog = _future_exog(target, history, horizon_hours)
    values: dict[pd.Timestamp, float] = dict(zip(history["datetime"], history[target_col]))

    rows = []
    for rec in exog.to_dict("records"):
        ts = rec["datetime"]
        rec[f"{target_col}_lag_1h"] = values[ts - pd.Timedelta(hours=1)]
        rec[f"{target_col}_lag_24h"] = values[ts - pd.Timedelta(hours=24)]
        rec[f"{target_col}_lag_168h"] = values[ts - pd.Timedelta(hours=168)]
        recent_24 = [values[ts - pd.Timedelta(hours=h)] for h in range(1, 25)]
        rec[f"{target_col}_roll_mean_24h"] = float(np.mean(recent_24))

        # DataFrame을 매 스텝 만들면 느려서(백테스트에서 수만 번 호출) numpy 1행으로 넘긴다.
        x_row = np.array([[rec[c] for c in feature_cols]], dtype=float)
        pred = max(float(model.predict(x_row)[0]), 0.0)  # 수요/발전량은 음수가 될 수 없음
        values[ts] = pred
        rows.append({"datetime": ts, "predicted": pred})
    return pd.DataFrame(rows)


def _direct_forecast(target: str, model, history: pd.DataFrame,
                     horizon_hours: int) -> pd.DataFrame:
    """기상 + 달력만으로 각 시각을 한 번에 예측한다 (과거 발전량 안 씀)."""
    exog = _future_exog(target, history, horizon_hours)
    pred = model.predict(exog[feature_columns(target)].to_numpy(dtype=float))
    if TARGET_MODE[target] == "cf":
        capacity = float(history["capacity_mw"].iloc[-1])  # 예측 시점의 최신 설비용량
        pred = pred * capacity
    return pd.DataFrame({"datetime": exog["datetime"], "predicted": np.clip(pred, 0.0, None)})


def forecast_path(target: str, model, history: pd.DataFrame, horizon_hours: int) -> pd.DataFrame:
    """target의 예측 방식(STRATEGY)에 맞게 미래 horizon_hours 시간을 예측한다."""
    if STRATEGY[target] == "recursive":
        return _recursive_forecast(target, model, history, horizon_hours)
    return _direct_forecast(target, model, history, horizon_hours)


def backtest(target: str, model, raw: pd.DataFrame,
             origins: list[pd.Timestamp], horizon_hours: int) -> dict[str, np.ndarray]:
    """여러 시작 시점(origin)에서 API와 똑같은 방식으로 horizon_hours만큼 예측해보고 결과를 모은다.

    origin 이전 데이터만 history로 넘기므로 미래 정답을 볼 수 없다.
    반환: pred / actual / capacity / hour — 모두 (n_origins, H). 실제값이 없는 칸은 NaN
    """
    indexed = raw.set_index("datetime")
    actual = indexed[TARGET_COLUMN[target]]
    capacity = indexed["capacity_mw"] if "capacity_mw" in indexed else None

    out: dict[str, list] = {"pred": [], "actual": [], "capacity": [], "hour": []}
    for origin in origins:
        hist = raw[raw["datetime"] < origin]
        f = forecast_path(target, model, hist, horizon_hours)
        out["pred"].append(f["predicted"].to_numpy())
        out["actual"].append(actual.reindex(f["datetime"]).to_numpy())
        out["hour"].append(f["datetime"].dt.hour.to_numpy())
        if capacity is not None:
            out["capacity"].append(capacity.reindex(f["datetime"]).ffill().bfill().to_numpy())
    result = {k: np.array(v) for k, v in out.items() if v}
    return result


# 하위 호환 이름
recursive_backtest = backtest


def _load_pi(target: str) -> dict | None:
    """train_lgbm.py가 저장한 PI margin + 시험구간 성적을 읽는다. 없으면 None."""
    path = SAVED_DIR / f"{target}_pi.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def interval(pi_info: dict, preds: np.ndarray,
             datetimes: pd.Series | pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    """pi.json의 margin 종류에 맞게 예측구간을 씌운다.
    horizon: "몇 시간 뒤"별 margin (수요) / hourly: "몇 시"별 margin (태양광·풍력)"""
    if pi_info["pi_type"] == "hourly":
        hours = pd.DatetimeIndex(datetimes).hour
        return apply_hourly_pi(preds, np.array(pi_info["margins"]), hours)
    return apply_horizon_pi(preds, np.array(pi_info["margins"]))


def run_forecast(target: str, start_date: str, end_date: str,
                  horizon_hours: int, model_name: str = "auto",
                  pi_method: str = "conformal") -> dict:
    """단일 target에 대한 예측 + PI + metrics를 전부 만들어서 반환.

    metrics: pi.json이 있으면 "학습에 안 쓴 시험 구간에서 API와 같은 방식으로 예측한" 성적
             (24h 이하 요청이면 24h 성적, 그보다 길면 168h 성적)을 돌려준다.
    """
    model, model_used = _load_model(target, model_name)
    raw = get_mock_data(target, start=start_date, end=end_date)

    forecast_df = forecast_path(target, model, raw, horizon_hours)
    preds = forecast_df["predicted"].to_numpy()

    pi_info = _load_pi(target)
    if pi_info is not None and "pi_type" in pi_info:
        lower, upper = interval(pi_info, preds, forecast_df["datetime"])
        key = "24h" if horizon_hours <= 24 else "168h"
        metrics = dict(pi_info["test_metrics"][key])
    else:
        # 폴백(pi.json 없음): 학습 데이터의 마지막 15% 오차로 단일 margin
        target_col = TARGET_COLUMN[target]
        df = build_features(raw, target_col)
        calib = df.iloc[int(len(df) * 0.85):]
        calib_pred = model.predict(calib[feature_columns(target)].to_numpy(dtype=float))
        if TARGET_MODE[target] == "cf":
            calib_pred = calib_pred * calib["capacity_mw"].to_numpy()
        predict_with_pi = fit_conformal(calib[target_col].values, calib_pred, confidence=0.9)
        calib_lower, calib_upper = predict_with_pi(calib_pred)
        capacity = calib["capacity_mw"].to_numpy() if "capacity_mw" in calib else None
        metrics = evaluate(target, calib[target_col].values, calib_pred, calib_lower, calib_upper,
                           capacity=capacity)
        lower, upper = predict_with_pi(preds)
        lower = np.maximum(lower, 0.0)

    forecast_df["lower_bound"] = lower
    forecast_df["upper_bound"] = upper

    points = [
        {
            "datetime": r.datetime,
            "predicted": round(r.predicted, 1),
            "lower_bound": round(r.lower_bound, 1),
            "upper_bound": round(r.upper_bound, 1),
        }
        for r in forecast_df.itertuples()
    ]

    return {
        "model_used": model_used,
        "pi_method": pi_method,
        "metrics": {k: v for k, v in metrics.items() if isinstance(v, (int, float))},
        "points": points,
    }


if __name__ == "__main__":
    result = run_forecast("demand", start_date="2025-07-01", end_date="2026-10-01",
                          horizon_hours=24)
    print(f"model_used: {result['model_used']}")
    print(f"metrics: {result['metrics']}")
    print(f"points[:3]: {json.dumps(result['points'][:3], indent=2, default=str)}")
