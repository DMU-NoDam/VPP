"""
예측구간(Prediction Interval) 계산 — Conformal Prediction 방식

원리:
    1) 모델이 한 번도 학습에 쓰지 않은 별도 구간(calibration set)에서
       "|실제값 - 예측값|" 오차들을 모은다.
    2) 그 오차 분포의 90% 지점 값(margin)을 구한다.
    3) 새로운 예측값에 [-margin, +margin]을 씌우면 90% 예측구간이 된다.

모델 종류(LightGBM/LSTM)와 무관하게 "예측값 - 실제값" 오차만 있으면
적용할 수 있어서, quantile regression처럼 모델을 따로 학습할 필요가 없다.
"""

from __future__ import annotations

import numpy as np


def compute_conformal_margin(y_true_calib: np.ndarray, y_pred_calib: np.ndarray,
                              confidence: float = 0.9) -> float:
    """calibration set의 오차 분포에서 confidence(기본 90%) 지점 margin을 구한다."""
    y_true_calib = np.asarray(y_true_calib, dtype=float)
    y_pred_calib = np.asarray(y_pred_calib, dtype=float)
    residuals = np.abs(y_true_calib - y_pred_calib)
    return float(np.quantile(residuals, confidence))


def apply_pi(y_pred: np.ndarray, margin: float) -> tuple[np.ndarray, np.ndarray]:
    """점 예측값에 margin을 씌워서 (하한, 상한)을 만든다."""
    y_pred = np.asarray(y_pred, dtype=float)
    lower = y_pred - margin
    upper = y_pred + margin
    return lower, upper


def fit_conformal(y_true_calib: np.ndarray, y_pred_calib: np.ndarray,
                   confidence: float = 0.9):
    """margin을 계산해서, 그 margin을 물고 있는 예측 함수를 돌려준다.

    사용 예:
        predict_with_pi = fit_conformal(y_true_calib, y_pred_calib)
        lower, upper = predict_with_pi(new_pred)
    """
    margin = compute_conformal_margin(y_true_calib, y_pred_calib, confidence)

    def predict_with_pi(y_pred: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return apply_pi(y_pred, margin)

    predict_with_pi.margin = margin  # 나중에 margin 값 자체가 필요할 때를 위해 노출
    return predict_with_pi


if __name__ == "__main__":
    import sys
    from pathlib import Path

    import lightgbm as lgb
    import pandas as pd

    sys.path.append(str(Path(__file__).resolve().parent))
    from data_client import get_mock_data
    from features import build_features
    from metrics import coverage

    print("=" * 60)
    print("실제 저장된 demand LightGBM 모델로 conformal PI 검증")
    print("=" * 60)

    target_col = "demand_mw"
    feature_cols = ["hour", "dayofweek", "is_weekend",
                     "demand_mw_lag_1h", "demand_mw_lag_24h", "demand_mw_lag_168h",
                     "demand_mw_roll_mean_24h"]

    raw = get_mock_data("demand", start="2025-06-01", end="2026-01-01")
    df = build_features(raw, target_col)

    # 3구간으로 시간순 분할: train(학습) / calib(PI margin 계산용) / test(최종 검증)
    n = len(df)
    train = df.iloc[: int(n * 0.7)]
    calib = df.iloc[int(n * 0.7): int(n * 0.85)]
    test = df.iloc[int(n * 0.85):]
    print(f"train {len(train)} / calib {len(calib)} / test {len(test)}")

    model = lgb.Booster(model_file=str(Path(__file__).parent / "models/saved/demand_lgbm.txt"))

    calib_pred = model.predict(calib[feature_cols])
    predict_with_pi = fit_conformal(calib[target_col].values, calib_pred, confidence=0.9)
    print(f"conformal margin: {predict_with_pi.margin:.1f} MW")

    test_pred = model.predict(test[feature_cols])
    lower, upper = predict_with_pi(test_pred)

    cov = coverage(test[target_col].values, lower, upper)
    print(f"test set PI coverage: {cov:.1f}%  (목표: 85% 이상)")

    sample = pd.DataFrame({
        "actual": test[target_col].values[:5].round(1),
        "predicted": test_pred[:5].round(1),
        "lower": lower[:5].round(1),
        "upper": upper[:5].round(1),
    })
    print(sample)
