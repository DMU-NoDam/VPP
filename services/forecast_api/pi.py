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


# --- 예측 시간대별(horizon-wise) conformal --------------------------------------
#
# 위 방식은 "1시간 앞 예측"의 오차로 margin을 정한다. 그런데 API는 1시간씩 이어서
# 24~168시간을 예측하므로(recursive), 먼 미래일수록 오차가 쌓여 margin이 모자란다.
# 그래서 calibration 구간에서 실제로 이어서 예측해보고, "h시간 뒤 예측"의 오차로
# h번째 margin을 따로 정한다 → 가까운 미래는 좁고, 먼 미래는 넓은 구간이 된다.


def conformal_quantile(residuals: np.ndarray, confidence: float = 0.9) -> float:
    """split conformal의 유한표본 보정 분위수: ceil((n+1)·conf)/n 지점.
    단순 np.quantile(conf)보다 살짝 보수적이라 커버리지가 목표 아래로 덜 떨어진다."""
    r = np.sort(np.asarray(residuals, dtype=float))
    n = len(r)
    if n == 0:
        return float("nan")
    q = min(1.0, np.ceil((n + 1) * confidence) / n)
    return float(np.quantile(r, q, method="higher"))


def compute_horizon_margins(abs_residuals: np.ndarray, confidence: float = 0.9,
                            pool: int = 3) -> np.ndarray:
    """(n_origins, H) 절대오차 행렬 -> 길이 H의 margin 배열.

    - h번째 margin은 h±pool 시간 오차를 묶어서 분위수를 구한다 (표본 수 확보 + 매끄럽게).
    - 먼 미래가 가까운 미래보다 좁아지지 않도록 누적 최댓값으로 단조 증가시킨다.
    """
    res = np.asarray(abs_residuals, dtype=float)
    n_steps = res.shape[1]
    margins = np.empty(n_steps)
    for h in range(n_steps):
        lo, hi = max(0, h - pool), min(n_steps, h + pool + 1)
        window = res[:, lo:hi].ravel()
        margins[h] = conformal_quantile(window[~np.isnan(window)], confidence)
    return np.maximum.accumulate(margins)


def compute_hourly_margins(abs_residuals: np.ndarray, hours: np.ndarray,
                           confidence: float = 0.9, pool: int = 1) -> np.ndarray:
    """하루 중 시간대(0~23시)별 margin 24개.

    태양광/풍력은 이어서 예측하지 않아서(direct) "몇 시간 뒤"와 오차가 무관하고,
    대신 "몇 시"에 따라 오차 크기가 크게 다르다 (태양광은 밤 0, 한낮 최대).
    h시 margin은 h±pool시 오차를 묶어서(자정 넘어 순환) 분위수를 구한다.
    """
    r = np.asarray(abs_residuals, dtype=float)
    hrs = np.asarray(hours, dtype=int)
    margins = np.empty(24)
    for h in range(24):
        near = [(h + d) % 24 for d in range(-pool, pool + 1)]
        window = r[np.isin(hrs, near)]
        margins[h] = conformal_quantile(window[~np.isnan(window)], confidence)
    return margins


def apply_hourly_pi(y_pred: np.ndarray, margins_by_hour: np.ndarray, hours: np.ndarray,
                    floor: float | None = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """i번째 예측값에 그 시각(hours[i])의 margin을 씌운다."""
    y_pred = np.asarray(y_pred, dtype=float)
    m = np.asarray(margins_by_hour, dtype=float)[np.asarray(hours, dtype=int)]
    lower, upper = y_pred - m, y_pred + m
    if floor is not None:
        lower = np.maximum(lower, floor)
    return lower, upper


def apply_horizon_pi(y_pred: np.ndarray, margins: np.ndarray,
                     floor: float | None = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """i번째 예측값에 margins[i]를 씌운다. floor가 있으면 하한을 그 값 아래로 내리지 않는다
    (수요/발전량은 음수가 될 수 없음)."""
    y_pred = np.asarray(y_pred, dtype=float)
    m = np.asarray(margins, dtype=float)[: len(y_pred)]
    lower, upper = y_pred - m, y_pred + m
    if floor is not None:
        lower = np.maximum(lower, floor)
    return lower, upper


# --- "몇 시간 뒤 × 몇 시" 가변 폭 (normalized conformal) --------------------------
#
# 이어서 예측(recursive)하는 모델은 "몇 시간 뒤"만으로 폭을 정하면, 오차가 큰 낮 시간과
# 오차가 작은 새벽에 같은 폭을 씌우게 된다 (수요는 맑은/흐린 날 낮 오차가 특히 크다).
# 그래서 폭 = q × (몇 시간 뒤 크기) × (몇 시 배율) 로 나눠서 정한다.
#   1) horizon_scale[h]: h시간 뒤 예측의 평균 절대오차 (h±pool 묶음, 단조 증가)
#   2) hour_scale[k]   : 오차를 horizon_scale로 나눈 값의 k시 평균 (평균 1이 되도록 정규화)
#   3) q               : |오차| / (horizon_scale × hour_scale) 의 conformal 분위수
# q를 calibration 오차에서 한 번에 정하므로 전체 커버리지 보장(split conformal)은 그대로이고,
# 폭만 어려운 시간대에 넓게, 쉬운 시간대에 좁게 나뉜다.


def target_hours(origins, n_steps: int) -> np.ndarray:
    """(n_origins, n_steps) 각 예측 칸이 가리키는 시각(0~23시). 첫 칸 = origin 시각."""
    start = np.array([int(o.hour) for o in origins])
    return (start[:, None] + np.arange(n_steps)[None, :]) % 24


def compute_horizon_hour_pi(abs_residuals: np.ndarray, hours: np.ndarray,
                            confidence: float = 0.9, pool: int = 3,
                            hour_pool: int = 1) -> dict:
    """(n_origins, H) 절대오차 + 같은 모양의 시각(0~23) -> 가변 폭 PI 파라미터 dict."""
    res = np.asarray(abs_residuals, dtype=float)
    hrs = np.asarray(hours, dtype=int)
    n_steps = res.shape[1]

    horizon_scale = np.empty(n_steps)
    for h in range(n_steps):
        lo, hi = max(0, h - pool), min(n_steps, h + pool + 1)
        horizon_scale[h] = np.nanmean(res[:, lo:hi])
    horizon_scale = np.maximum.accumulate(np.maximum(horizon_scale, 1e-9))

    rel = res / horizon_scale[None, :]
    hour_scale = np.empty(24)
    for k in range(24):
        near = [(k + d) % 24 for d in range(-hour_pool, hour_pool + 1)]
        vals = rel[np.isin(hrs, near)]
        hour_scale[k] = np.nanmean(vals) if np.isfinite(vals).any() else 1.0
    hour_scale = np.maximum(hour_scale, 1e-6)
    hour_scale = hour_scale / hour_scale.mean()

    scale = horizon_scale[None, :] * hour_scale[hrs]
    score = (res / scale).ravel()
    q = conformal_quantile(score[~np.isnan(score)], confidence)
    return {"horizon_scale": horizon_scale, "hour_scale": hour_scale, "q": q,
            "margins": q * horizon_scale}  # margins: 하루 평균 배율(1) 기준 참고값


def apply_horizon_hour_pi(y_pred: np.ndarray, horizon_scale: np.ndarray,
                          hour_scale: np.ndarray, q: float, hours: np.ndarray,
                          floor: float | None = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """i번째 예측값(시각 hours[i])에 q × horizon_scale[i] × hour_scale[시각] 폭을 씌운다."""
    y_pred = np.asarray(y_pred, dtype=float)
    hz = np.asarray(horizon_scale, dtype=float)[: len(y_pred)]
    hs = np.asarray(hour_scale, dtype=float)[np.asarray(hours, dtype=int)]
    m = q * hz * hs
    lower, upper = y_pred - m, y_pred + m
    if floor is not None:
        lower = np.maximum(lower, floor)
    return lower, upper


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

    from features import feature_columns

    target_col = "demand_mw"
    feature_cols = feature_columns("demand")

    raw = get_mock_data("demand", start="2025-07-01", end="2026-10-01")
    df = build_features(raw, target_col)

    # 3구간으로 시간순 분할: train(학습) / calib(PI margin 계산용) / test(최종 검증)
    n = len(df)
    train = df.iloc[: int(n * 0.7)]
    calib = df.iloc[int(n * 0.7): int(n * 0.85)]
    test = df.iloc[int(n * 0.85):]
    print(f"train {len(train)} / calib {len(calib)} / test {len(test)}")

    model = lgb.Booster(model_file=str(Path(__file__).parent / "models/saved/demand_lgbm.txt"))

    calib_pred = model.predict(calib[feature_cols].to_numpy(dtype=float))
    predict_with_pi = fit_conformal(calib[target_col].values, calib_pred, confidence=0.9)
    print(f"conformal margin: {predict_with_pi.margin:.1f} MW")

    test_pred = model.predict(test[feature_cols].to_numpy(dtype=float))
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
