"""
LightGBM 학습 스크립트 (오프라인 실행 전용)

forecast_api가 요청마다 재학습하지 않도록, 이 스크립트는 따로 실행해서
모델을 saved/ 폴더에 파일로 저장해둔다. API는 이 저장된 파일을 불러와
예측만 수행한다.

실행:
    python3 models/train_lgbm.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[1]))  # forecast_api/ 를 import 경로에 추가

from data_client import get_mock_data
from features import build_features

SAVED_DIR = Path(__file__).resolve().parent / "saved"
SAVED_DIR.mkdir(exist_ok=True)

# feature 컬럼 정의: target별로 쓸 수 있는 컬럼이 조금씩 다르다
FEATURE_COLUMNS = {
    "demand": ["hour", "dayofweek", "is_weekend",
               "demand_mw_lag_1h", "demand_mw_lag_24h", "demand_mw_lag_168h",
               "demand_mw_roll_mean_24h"],
    "solar": ["hour", "dayofweek", "is_weekend", "irradiance",
              "solar_mw_lag_1h", "solar_mw_lag_24h", "solar_mw_lag_168h",
              "solar_mw_roll_mean_24h"],
    "wind": ["hour", "dayofweek", "is_weekend", "wind_speed",
             "wind_mw_lag_1h", "wind_mw_lag_24h", "wind_mw_lag_168h",
             "wind_mw_roll_mean_24h"],
}
TARGET_COLUMN = {"demand": "demand_mw", "solar": "solar_mw", "wind": "wind_mw"}


def mape(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.mean(np.abs((y_true - y_pred) / y_true)) * 100)


def nmae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """정규화 MAE. 실제값이 0에 가까워 MAPE가 발산하는 태양광/풍력에 사용
    (스펙에서도 수요는 MAPE, 태양광/풍력은 nMAE 기준을 쓴다)."""
    return float(np.mean(np.abs(y_true - y_pred)) / np.mean(y_true) * 100)


# target별로 어떤 평가지표를 쓸지 (스펙 기준: 수요=MAPE, 재생에너지=nMAE)
METRIC_FN = {"demand": mape, "solar": nmae, "wind": nmae}
METRIC_NAME = {"demand": "MAPE", "solar": "nMAE", "wind": "nMAE"}


def train_one(target: str, start: str = "2025-06-01", end: str = "2026-01-01") -> None:
    print("=" * 60)
    print(f"target = {target}")
    print("=" * 60)

    target_col = TARGET_COLUMN[target]
    feature_cols = FEATURE_COLUMNS[target]

    # 1) 데이터 가져오기 + feature 손질 (지금은 목업, 나중에 실데이터로 자동 교체됨)
    raw = get_mock_data(target, start=start, end=end)
    df = build_features(raw, target_col)

    # 2) 시간순 train/test 분할 (마지막 2주를 test로 사용, 랜덤 분할 금지)
    split_point = df["datetime"].max() - pd.Timedelta(days=14)
    train = df[df["datetime"] < split_point]
    test = df[df["datetime"] >= split_point]

    X_train, y_train = train[feature_cols], train[target_col]
    X_test, y_test = test[feature_cols], test[target_col]

    print(f"전체 {len(df)}행 -> train {len(train)}행 / test {len(test)}행")

    # 3) 학습
    model = lgb.LGBMRegressor(
        n_estimators=300,
        learning_rate=0.05,
        num_leaves=31,
        verbosity=-1,
    )
    model.fit(X_train, y_train)

    # 4) test로 검증 (target에 맞는 평가지표 사용)
    pred = model.predict(X_test)
    metric_fn = METRIC_FN[target]
    score = metric_fn(y_test.values, pred)
    print(f"Test {METRIC_NAME[target]}: {score:.2f}%")

    # 5) 실제값 vs 예측값 몇 개 비교
    compare = pd.DataFrame({
        "datetime": test["datetime"].values[:5],
        "actual": y_test.values[:5],
        "predicted": pred[:5].round(1),
    })
    print(compare)

    # 6) 모델 저장 (API가 나중에 이 파일을 불러와서 씀)
    save_path = SAVED_DIR / f"{target}_lgbm.txt"
    model.booster_.save_model(str(save_path))
    print(f"모델 저장 완료: {save_path}\n")


if __name__ == "__main__":
    for t in ["demand", "solar", "wind"]:
        train_one(t)
