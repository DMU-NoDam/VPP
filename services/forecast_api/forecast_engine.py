"""
예측 엔진 — data_client / features / models / metrics / pi 를 조립해서
"미래 horizon_hours 시간의 예측값 + 90% 구간"을 만들어내는 핵심 로직.

router.py는 이 모듈을 호출하기만 하고, HTTP 관련 코드는 여기 두지 않는다.
"""

from __future__ import annotations

from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from data_client import get_mock_data
from features import build_features
from metrics import evaluate
from models.train_lgbm import FEATURE_COLUMNS, TARGET_COLUMN
from pi import fit_conformal

SAVED_DIR = Path(__file__).resolve().parent / "models" / "saved"

# 미래 시점의 날씨(irradiance/wind_speed)는 원래 기상청 "예보"를 써야 하지만,
# 지금은 A의 실데이터 연동 전이라 get_mock_data로 미래 구간의 합성 날씨를
# 그대로 가져다 "예보값"처럼 사용한다. 실제 데이터 연동 시 이 부분을
# 기상청 예보 조회로 바꿔치기하면 된다.
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
    if weather_col is not None:
        # 미래 구간의 "예보값" 자리 — 지금은 목업으로 대체 (위 주석 참고)
        future_weather = get_mock_data(
            target,
            start=str(future_start.date()),
            end=str((future_start + pd.Timedelta(hours=horizon_hours + 24)).date()),
        ).set_index("datetime")[weather_col]

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
            row[weather_col] = float(future_weather.get(ts, future_weather.mean()))

        X = pd.DataFrame([row])[feature_cols]
        pred = float(model.predict(X)[0])
        pred = max(pred, 0.0)  # 발전량/수요는 음수가 될 수 없음

        values[ts] = pred  # 다음 스텝의 lag 계산에 이 예측값을 그대로 사용
        rows.append({"datetime": ts, "predicted": pred})

    return pd.DataFrame(rows)


def run_forecast(target: str, start_date: str, end_date: str,
                  horizon_hours: int, model_name: str = "auto",
                  pi_method: str = "conformal") -> dict:
    """단일 target에 대한 예측 + PI + metrics를 전부 만들어서 반환."""
    target_col = TARGET_COLUMN[target]
    feature_cols = FEATURE_COLUMNS[target]

    model, model_used = _load_model(target, model_name)

    raw = get_mock_data(target, start=start_date, end=end_date)
    df = build_features(raw, target_col)

    # calib/test로 나눠 conformal margin 계산 + 그 margin의 신뢰도(coverage) 검증
    n = len(df)
    calib = df.iloc[int(n * 0.85):]
    calib_pred = model.predict(calib[feature_cols])
    predict_with_pi = fit_conformal(calib[target_col].values, calib_pred, confidence=0.9)

    # 검증용으로 calib 자체 coverage도 같이 계산해서 metrics에 포함
    calib_lower, calib_upper = predict_with_pi(calib_pred)
    metrics = evaluate(target, calib[target_col].values, calib_pred, calib_lower, calib_upper)

    # 실제 미래 예측 (recursive)
    forecast_df = _recursive_forecast(target, target_col, feature_cols, model, raw, horizon_hours)
    lower, upper = predict_with_pi(forecast_df["predicted"].values)
    forecast_df["lower_bound"] = np.maximum(lower, 0.0)
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
    import json

    result = run_forecast("demand", start_date="2025-06-01", end_date="2026-01-01", horizon_hours=24)
    print(f"model_used: {result['model_used']}")
    print(f"metrics: {result['metrics']}")
    print(f"points[:3]: {json.dumps(result['points'][:3], indent=2, default=str)}")
