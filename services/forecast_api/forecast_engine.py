"""
예측 엔진 — data_client / features / models / metrics / pi 를 조립해서
"미래 horizon_hours 시간의 예측값 + 90% 구간"을 만들어내는 핵심 로직.

router.py는 이 모듈을 호출하기만 하고, HTTP 관련 코드는 여기 두지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from data_client import get_mock_data, get_weather
from features import build_features
from metrics import evaluate
from models.train_lgbm import FEATURE_COLUMNS, TARGET_COLUMN
from pi import apply_horizon_pi, fit_conformal

SAVED_DIR = Path(__file__).resolve().parent / "models" / "saved"

# 미래 시점의 날씨(irradiance/wind_speed)는 원래 기상청 "예보"를 써야 하지만,
# 예보 API가 아직 없어서 data_client.get_weather로 해당 시각의 "관측값"을
# 예보값 대용으로 쓴다. 예보 API가 생기면 get_weather만 바꿔치기하면 된다.
WEATHER_COLUMN = {"solar": "irradiance", "wind": "wind_speed"}


def _load_model(target: str, model_name: str) -> tuple[lgb.Booster, str]:
    """model_name이 "auto"면 지금 실제로 파일이 있는 lightgbm을 쓴다.
    (LSTM은 본인 컴퓨터에서 학습 완료 후 saved/에 파일이 생겨야 로드 가능)
    """
    if model_name == "lstm":
        lstm_path = SAVED_DIR / f"{target}_lstm.pt"
        if not lstm_path.exists():
            raise FileNotFoundError(
                f"{lstm_path} 없음 — 본인 컴퓨터에서 models/train_lstm.py 먼저 실행 필요"
            )
        raise NotImplementedError("LSTM 서빙 로직은 아직 미구현 (다음 단계)")

    lgbm_path = SAVED_DIR / f"{target}_lgbm.txt"
    if not lgbm_path.exists():
        raise FileNotFoundError(f"{lgbm_path} 없음 — models/train_lgbm.py 먼저 실행 필요")
    return lgb.Booster(model_file=str(lgbm_path)), "lightgbm"


def _time_features(ts: pd.Timestamp) -> dict:
    return {
        "hour": ts.hour,
        "dayofweek": ts.dayofweek,
        "is_weekend": int(ts.dayofweek in (5, 6)),
    }


def _recursive_forecast(
    target: str,
    target_col: str,
    feature_cols: list[str],
    model: lgb.Booster,
    history: pd.DataFrame,
    horizon_hours: int,
) -> pd.DataFrame:
    """1시간씩 순차적으로 미래를 예측하면서, 예측값을 다음 스텝의 lag로 재사용한다.

    history: build_features 적용 *전*의 원본 시계열 (datetime, target_col [, weather_col])
             과거 168시간 이상 있어야 lag_168h를 채울 수 있다.
    """
    # 값 조회를 빠르게 하기 위해 datetime -> target_col 값 dict로 관리
    values: dict[pd.Timestamp, float] = dict(zip(history["datetime"], history[target_col]))

    weather_col = WEATHER_COLUMN.get(target)
    future_start = history["datetime"].max() + pd.Timedelta(hours=1)

    future_weather = None
    hourly_profile = None
    if weather_col is not None:
        # 미래 구간의 "예보값" 자리 — 예보 API가 없어서 해당 시각의 관측값을 대용으로 쓴다.
        future_end = future_start + pd.Timedelta(hours=horizon_hours)
        future_weather = get_weather(target, start=str(future_start), end=str(future_end))
        # 관측값도 없는 시각(데이터 끝 이후)은 과거 데이터의 "시간대별 평균"으로 채운다.
        hourly_profile = history.groupby(history["datetime"].dt.hour)[weather_col].mean()

    rows = []
    for step in range(horizon_hours):
        ts = future_start + pd.Timedelta(hours=step)

        row = _time_features(ts)
        row[f"{target_col}_lag_1h"] = values[ts - pd.Timedelta(hours=1)]
        row[f"{target_col}_lag_24h"] = values[ts - pd.Timedelta(hours=24)]
        row[f"{target_col}_lag_168h"] = values[ts - pd.Timedelta(hours=168)]

        recent_24 = [values[ts - pd.Timedelta(hours=h)] for h in range(1, 25)]
        row[f"{target_col}_roll_mean_24h"] = float(np.mean(recent_24))

        if weather_col is not None:
            value = future_weather.get(ts, np.nan)
            if pd.isna(value):
                value = hourly_profile.get(ts.hour, 0.0)
            row[weather_col] = float(value)

        # DataFrame을 매 스텝 만들면 느려서(백테스트에서 수만 번 호출) numpy 1행으로 넘긴다.
        X = np.array([[row[c] for c in feature_cols]], dtype=float)
        pred = float(model.predict(X)[0])
        pred = max(pred, 0.0)  # 발전량/수요는 음수가 될 수 없음

        values[ts] = pred  # 다음 스텝의 lag 계산에 이 예측값을 그대로 사용
        rows.append({"datetime": ts, "predicted": pred})

    return pd.DataFrame(rows)


def recursive_backtest(target: str, model, raw: pd.DataFrame,
                       origins: list[pd.Timestamp], horizon_hours: int) -> dict[str, np.ndarray]:
    """여러 시작 시점(origin)에서 실제로 horizon_hours만큼 이어서 예측해보고 결과를 모은다.

    origin 이전 데이터만 보고 예측하므로 API가 실제로 하는 일과 똑같은 조건이다.
    반환: {"pred": (n_origins, H), "actual": (n_origins, H)} — 실제값이 없는 칸은 NaN
    """
    target_col = TARGET_COLUMN[target]
    feature_cols = FEATURE_COLUMNS[target]
    actual = raw.set_index("datetime")[target_col]

    preds, actuals = [], []
    for origin in origins:
        hist = raw[raw["datetime"] < origin]
        f = _recursive_forecast(target, target_col, feature_cols, model, hist, horizon_hours)
        preds.append(f["predicted"].to_numpy())
        actuals.append(actual.reindex(f["datetime"]).to_numpy())
    return {"pred": np.array(preds), "actual": np.array(actuals)}


def _load_pi(target: str) -> dict | None:
    """train_lgbm.py가 저장한 시간대별 PI margin + 시험구간 성적을 읽는다. 없으면 None."""
    path = SAVED_DIR / f"{target}_pi.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def run_forecast(target: str, start_date: str, end_date: str,
                  horizon_hours: int, model_name: str = "auto",
                  pi_method: str = "conformal") -> dict:
    """단일 target에 대한 예측 + PI + metrics를 전부 만들어서 반환.

    PI: saved/{target}_pi.json(학습 때 계산한 시간대별 margin)이 있으면 그걸 쓰고,
        없으면 예전 방식(1시간 앞 예측 오차 기반 단일 margin)으로 대체한다.
    metrics: pi.json이 있으면 "학습에 안 쓴 시험 구간에서 이어서 예측한" 성적
             (24h 이하 요청이면 24h 성적, 그보다 길면 168h 성적)을 돌려준다.
    """
    target_col = TARGET_COLUMN[target]
    feature_cols = FEATURE_COLUMNS[target]

    model, model_used = _load_model(target, model_name)
    raw = get_mock_data(target, start=start_date, end=end_date)

    # 실제 미래 예측 (recursive)
    forecast_df = _recursive_forecast(target, target_col, feature_cols, model, raw, horizon_hours)
    preds = forecast_df["predicted"].to_numpy()

    pi_info = _load_pi(target)
    if pi_info is not None and len(pi_info["margins"]) >= horizon_hours:
        lower, upper = apply_horizon_pi(preds, np.array(pi_info["margins"]))
        key = "24h" if horizon_hours <= 24 else "168h"
        metrics = dict(pi_info["test_metrics"][key])
    else:
        # 폴백: 1시간 앞 예측 오차 기반 (먼 미래에서는 구간이 좁게 나온다)
        df = build_features(raw, target_col)
        calib = df.iloc[int(len(df) * 0.85):]
        calib_pred = model.predict(calib[feature_cols])
        predict_with_pi = fit_conformal(calib[target_col].values, calib_pred, confidence=0.9)
        calib_lower, calib_upper = predict_with_pi(calib_pred)
        metrics = evaluate(target, calib[target_col].values, calib_pred, calib_lower, calib_upper)
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
    result = run_forecast("demand", start_date="2025-07-01", end_date="2026-10-01", horizon_hours=24)
    print(f"model_used: {result['model_used']}")
    print(f"metrics: {result['metrics']}")
    print(f"points[:3]: {json.dumps(result['points'][:3], indent=2, default=str)}")
