"""
LSTM 학습 스크립트 (오프라인 실행 전용)

LightGBM(train_lgbm.py)은 사람이 미리 계산한 힌트 컬럼(lag, rolling 등)을
보고 배우는 방식이었다면, LSTM은 손질 없이 "최근 24시간 값의 흐름"을
그대로 순서대로 읽어서 다음 값을 예측하는 방식이다.

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
LOOKBACK = 24  # 최근 24시간을 보고 다음 1시간을 예측


def mape(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.mean(np.abs((y_true - y_pred) / y_true)) * 100)


def nmae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.mean(np.abs(y_true - y_pred)) / np.mean(y_true) * 100)


METRIC_FN = {"demand": mape, "solar": nmae, "wind": nmae}
METRIC_NAME = {"demand": "MAPE", "solar": "nMAE", "wind": "nMAE"}


def make_sequences(values: np.ndarray, lookback: int) -> tuple[np.ndarray, np.ndarray]:
    """1차원 시계열을 (과거 lookback개 -> 다음 1개) 쌍들로 재구성.

    예: values = [10,11,12,13,14], lookback=3
        X[0] = [10,11,12], y[0] = 13
        X[1] = [11,12,13], y[1] = 14
    """
    X, y = [], []
    for i in range(len(values) - lookback):
        X.append(values[i:i + lookback])
        y.append(values[i + lookback])
    return np.array(X), np.array(y)


class SimpleLSTM(nn.Module):
    def __init__(self, hidden_size: int = 32):
        super().__init__()
        self.lstm = nn.LSTM(input_size=1, hidden_size=hidden_size, batch_first=True)
        self.head = nn.Linear(hidden_size, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (batch, lookback, 1)
        out, _ = self.lstm(x)
        last_step = out[:, -1, :]  # 마지막 시점의 은닉 상태만 사용
        return self.head(last_step).squeeze(-1)


def train_one(target: str, start: str = "2025-06-01", end: str = "2026-01-01",
              epochs: int = 30) -> float:
    print("=" * 60)
    print(f"target = {target}")
    print("=" * 60)

    target_col = TARGET_COLUMN[target]
    raw = get_mock_data(target, start=start, end=end).sort_values("datetime").reset_index(drop=True)

    # 1) 시간순 train/test 분할 (LightGBM과 동일하게 마지막 2주를 test로)
    split_point = raw["datetime"].max() - pd.Timedelta(days=14)
    train_raw = raw[raw["datetime"] < split_point]
    test_raw = raw[raw["datetime"] >= split_point]

    # 2) 정규화 (train 구간의 평균/표준편차만 사용 — test 정보가 새어들어가면 안 됨)
    mean, std = train_raw[target_col].mean(), train_raw[target_col].std()
    train_norm = (train_raw[target_col].values - mean) / std
    # test는 직전 LOOKBACK시간이 train 끝자락과 이어져야 하므로 앞부분을 이어붙임
    test_with_context = pd.concat([train_raw.tail(LOOKBACK), test_raw])[target_col].values
    test_norm = (test_with_context - mean) / std

    # 3) 슬라이딩 윈도우 시퀀스 생성
    X_train, y_train = make_sequences(train_norm, LOOKBACK)
    X_test, y_test = make_sequences(test_norm, LOOKBACK)

    X_train_t = torch.tensor(X_train, dtype=torch.float32).unsqueeze(-1)
    y_train_t = torch.tensor(y_train, dtype=torch.float32)
    X_test_t = torch.tensor(X_test, dtype=torch.float32).unsqueeze(-1)

    print(f"train 시퀀스 {len(X_train)}개 / test 시퀀스 {len(X_test)}개")

    # 4) 학습
    model = SimpleLSTM()
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

    # 5) test 예측 (정규화 되돌리기)
    model.eval()
    with torch.no_grad():
        pred_norm = model(X_test_t).numpy()
    pred = pred_norm * std + mean
    actual = y_test * std + mean

    metric_fn = METRIC_FN[target]
    score = metric_fn(actual, pred)
    print(f"Test {METRIC_NAME[target]}: {score:.2f}%  (학습시간 {elapsed:.1f}초)")

    compare = pd.DataFrame({
        "actual": actual[:5].round(1),
        "predicted": pred[:5].round(1),
    })
    print(compare)

    save_path = SAVED_DIR / f"{target}_lstm.pt"
    torch.save({"state_dict": model.state_dict(), "mean": mean, "std": std}, save_path)
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
