"""
LSTM 학습 스크립트 (오프라인 실행 전용)

LightGBM(train_lgbm.py)은 사람이 미리 계산한 힌트 컬럼(lag, rolling 등)을
보고 배우는 방식이었다면, LSTM은 손질 없이 "최근 24시간 값의 흐름"을
그대로 순서대로 읽어서 다음 값을 예측하는 방식이다.

다변량(multivariate) 지원: 태양광/풍력은 발전량 혼자만 보면 패턴이 거의 없고
날씨(일사량/풍속)가 원인이라서, 발전량 값뿐 아니라 그 시점의 날씨 값도
같이 시퀀스에 넣는다. 수요는 날씨 covariate 없이 단변량으로 그대로 둔다.

실행:
    python3 models/train_lstm.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from torch import nn

sys.path.append(str(Path(__file__).resolve().parents[1]))

from data_client import get_mock_data

SAVED_DIR = Path(__file__).resolve().parent / "saved"
SAVED_DIR.mkdir(exist_ok=True)

TARGET_COLUMN = {"demand": "demand_mw", "solar": "solar_mw", "wind": "wind_mw"}
# target 자신 외에 같이 넣어줄 날씨 covariate. 없으면(demand) 단변량으로 학습.
WEATHER_COLUMN = {"solar": "irradiance", "wind": "wind_speed"}
LOOKBACK = 24  # 최근 24시간을 보고 다음 1시간을 예측


def mape(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.mean(np.abs((y_true - y_pred) / y_true)) * 100)


def nmae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.mean(np.abs(y_true - y_pred)) / np.mean(y_true) * 100)


METRIC_FN = {"demand": mape, "solar": nmae, "wind": nmae}
METRIC_NAME = {"demand": "MAPE", "solar": "nMAE", "wind": "nMAE"}


def make_sequences(feature_matrix: np.ndarray, lookback: int) -> tuple[np.ndarray, np.ndarray]:
    """(n_timesteps, n_features) 배열을 (과거 lookback개 -> 다음 1개 target) 쌍으로 재구성.

    target(예측 대상)은 항상 feature_matrix의 0번째 열이라고 약속한다.
    예: feature_matrix가 [발전량, 풍속] 2열이면, y는 발전량 열의 다음 값.
    """
    X, y = [], []
    for i in range(len(feature_matrix) - lookback):
        X.append(feature_matrix[i:i + lookback])
        y.append(feature_matrix[i + lookback, 0])  # 0번째 열 = target
    return np.array(X), np.array(y)


class SimpleLSTM(nn.Module):
    def __init__(self, input_size: int = 1, hidden_size: int = 32):
        super().__init__()
        self.lstm = nn.LSTM(input_size=input_size, hidden_size=hidden_size, batch_first=True)
        self.head = nn.Linear(hidden_size, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (batch, lookback, input_size)
        out, _ = self.lstm(x)
        last_step = out[:, -1, :]  # 마지막 시점의 은닉 상태만 사용
        return self.head(last_step).squeeze(-1)


def _build_feature_matrix(df: pd.DataFrame, target_col: str, weather_col: str | None,
                           target_mean: float, target_std: float,
                           weather_stats: tuple[float, float] | None) -> np.ndarray:
    """target(+weather) 컬럼들을 각각 정규화해서 (n, n_features) 배열로 합친다."""
    target_norm = (df[target_col].values - target_mean) / target_std
    if weather_col is None:
        return target_norm.reshape(-1, 1)

    w_mean, w_std = weather_stats
    weather_norm = (df[weather_col].values - w_mean) / w_std
    return np.stack([target_norm, weather_norm], axis=-1)


def train_one(target: str, start: str = "2025-06-01", end: str = "2026-01-01",
              epochs: int = 30) -> float:
    print("=" * 60)
    weather_col = WEATHER_COLUMN.get(target)
    print(f"target = {target}  (다변량: {weather_col if weather_col else '아니오, 단변량'})")
    print("=" * 60)

    target_col = TARGET_COLUMN[target]
    raw = get_mock_data(target, start=start, end=end).sort_values("datetime").reset_index(drop=True)

    # 1) 시간순 train/test 분할 (LightGBM과 동일하게 마지막 2주를 test로)
    split_point = raw["datetime"].max() - pd.Timedelta(days=14)
    train_raw = raw[raw["datetime"] < split_point]
    test_raw = raw[raw["datetime"] >= split_point]
    # test 앞에 LOOKBACK시간만큼 train 끝자락을 이어붙여야 첫 test 시퀀스를 만들 수 있음
    test_with_context = pd.concat([train_raw.tail(LOOKBACK), test_raw])

    # 2) 정규화 통계는 train 구간에서만 계산 (test 정보 누출 방지)
    target_mean, target_std = train_raw[target_col].mean(), train_raw[target_col].std()
    weather_stats = None
    if weather_col is not None:
        weather_stats = (train_raw[weather_col].mean(), train_raw[weather_col].std())

    train_matrix = _build_feature_matrix(train_raw, target_col, weather_col, target_mean, target_std, weather_stats)
    test_matrix = _build_feature_matrix(test_with_context, target_col, weather_col, target_mean, target_std, weather_stats)

    # 3) 슬라이딩 윈도우 시퀀스 생성
    X_train, y_train = make_sequences(train_matrix, LOOKBACK)
    X_test, y_test = make_sequences(test_matrix, LOOKBACK)

    X_train_t = torch.tensor(X_train, dtype=torch.float32)
    y_train_t = torch.tensor(y_train, dtype=torch.float32)
    X_test_t = torch.tensor(X_test, dtype=torch.float32)

    input_size = X_train.shape[-1]
    print(f"train 시퀀스 {len(X_train)}개 / test 시퀀스 {len(X_test)}개 / input_size={input_size}")

    # 4) 학습
    model = SimpleLSTM(input_size=input_size)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
    loss_fn = nn.MSELoss()

    start_time = time.time()
    for epoch in range(epochs):
        optimizer.zero_grad()
        pred = model(X_train_t)
        loss = loss_fn(pred, y_train_t)
        loss.backward()
        optimizer.step()
        if (epoch + 1) % 10 == 0:
            print(f"  epoch {epoch + 1}/{epochs} - loss {loss.item():.4f}")
    elapsed = time.time() - start_time

    # 5) test 예측 (정규화 되돌리기, target 기준으로만)
    model.eval()
    with torch.no_grad():
        pred_norm = model(X_test_t).numpy()
    pred = pred_norm * target_std + target_mean
    actual = y_test * target_std + target_mean
    pred = np.clip(pred, 0, None)  # 발전량/수요는 음수 불가

    metric_fn = METRIC_FN[target]
    score = metric_fn(actual, pred)
    print(f"Test {METRIC_NAME[target]}: {score:.2f}%  (학습시간 {elapsed:.1f}초)")

    compare = pd.DataFrame({
        "actual": actual[:5].round(1),
        "predicted": pred[:5].round(1),
    })
    print(compare)

    save_path = SAVED_DIR / f"{target}_lstm.pt"
    torch.save({
        "state_dict": model.state_dict(),
        "input_size": input_size,
        "target_mean": target_mean, "target_std": target_std,
        "weather_col": weather_col, "weather_stats": weather_stats,
    }, save_path)
    print(f"모델 저장 완료: {save_path}\n")

    return score


if __name__ == "__main__":
    results = {}
    for t in ["demand", "solar", "wind"]:
        results[t] = train_one(t)

    print("=" * 60)
    print("LSTM 최종 결과 요약")
    print("=" * 60)
    for t, score in results.items():
        print(f"{t:8s} {METRIC_NAME[t]}: {score:.2f}%")
