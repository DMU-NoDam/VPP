"""
평가지표 모듈 (MAPE / nMAE / PI Coverage)

train_lgbm.py, train_lstm.py에 중복으로 들어있던 평가 함수를 여기 하나로
모아둔다. forecast_api의 응답(metrics 필드)을 채울 때도 이 모듈을 쓴다.

스펙 기준 (docs/api_spec.md, 과제 성능기준표와 반드시 맞춰야 함):
    수요 예측    : MAPE  24h ≤ 4%,  168h ≤ 7%
    태양광 예측  : nMAE ≤ 12%
    풍력 예측    : nMAE ≤ 15%
    90% PI       : Coverage ≥ 85%
"""

from __future__ import annotations

import numpy as np

# target별로 스펙에서 요구하는 주 평가지표 ("demand"는 MAPE, 재생에너지는 nMAE)
PRIMARY_METRIC = {"demand": "mape", "solar": "nmae", "wind": "nmae"}

# 성능 목표치 (그대로 스펙 표 값)
SPEC_THRESHOLDS = {
    "demand_mape_24h": 4.0,
    "demand_mape_168h": 7.0,
    "solar_nmae": 12.0,
    "wind_nmae": 15.0,
    "pi_coverage": 85.0,
}


def mape(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """평균 절대 백분율 오차(%).

    실제값이 0인 지점은 나눗셈이 발산하므로 제외하고 계산한다
    (태양광/풍력의 야간·무풍 시간대처럼 0이 흔한 타겟은 nMAE를 쓰는 게 맞고,
    MAPE는 주로 수요처럼 0이 거의 없는 타겟에 사용한다).
    """
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    mask = y_true != 0
    if not mask.any():
        return float("nan")
    return float(np.mean(np.abs((y_true[mask] - y_pred[mask]) / y_true[mask])) * 100)


def nmae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """정규화 평균절대오차(%) = MAE / 실제값 평균. 0이 섞여 있어도 안전하다."""
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    denom = np.mean(np.abs(y_true))
    if denom == 0:
        return float("nan")
    return float(np.mean(np.abs(y_true - y_pred)) / denom * 100)


def coverage(y_true: np.ndarray, lower: np.ndarray, upper: np.ndarray) -> float:
    """실제값이 [lower, upper] 구간 안에 들어간 비율(%)."""
    y_true = np.asarray(y_true, dtype=float)
    lower = np.asarray(lower, dtype=float)
    upper = np.asarray(upper, dtype=float)
    inside = (y_true >= lower) & (y_true <= upper)
    return float(np.mean(inside) * 100)


def evaluate(
    target: str,
    y_true: np.ndarray,
    y_pred: np.ndarray,
    lower: np.ndarray | None = None,
    upper: np.ndarray | None = None,
) -> dict[str, float]:
    """forecast_api 응답의 "metrics" 필드에 그대로 넣을 수 있는 dict를 만든다."""
    result = {
        "mape": mape(y_true, y_pred),
        "nmae": nmae(y_true, y_pred),
    }
    if lower is not None and upper is not None:
        result["coverage"] = coverage(y_true, lower, upper)

    result["primary_metric"] = PRIMARY_METRIC[target]
    result["primary_value"] = result[PRIMARY_METRIC[target]]
    return result


def check_spec(target: str, horizon_h: int, metrics: dict[str, float]) -> bool:
    """방금 나온 metrics가 스펙 기준을 통과하는지 True/False로 알려준다."""
    if target == "demand":
        key = "demand_mape_24h" if horizon_h <= 24 else "demand_mape_168h"
        ok = metrics["mape"] <= SPEC_THRESHOLDS[key]
    elif target == "solar":
        ok = metrics["nmae"] <= SPEC_THRESHOLDS["solar_nmae"]
    elif target == "wind":
        ok = metrics["nmae"] <= SPEC_THRESHOLDS["wind_nmae"]
    else:
        raise ValueError(f"알 수 없는 target: {target}")

    if "coverage" in metrics:
        ok = ok and metrics["coverage"] >= SPEC_THRESHOLDS["pi_coverage"]
    return ok


if __name__ == "__main__":
    # 간단한 자체 검증: 0이 섞인 데이터에서도 안 터지는지 확인
    y_true = np.array([100, 0, 50, 0, 200])
    y_pred = np.array([110, 5, 45, 2, 190])
    lower = y_pred - 20
    upper = y_pred + 20

    print("mape:", mape(y_true, y_pred))       # 0인 지점 제외하고 계산됨
    print("nmae:", nmae(y_true, y_pred))       # 0이 섞여도 안전
    print("coverage:", coverage(y_true, lower, upper))

    result = evaluate("wind", y_true, y_pred, lower, upper)
    print("evaluate():", result)
    print("스펙 통과 여부:", check_spec("wind", 24, result))
