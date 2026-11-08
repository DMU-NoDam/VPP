"""
LSTM 학습 + LightGBM과 비교 스크립트 (오프라인 실행 전용, torch 필요)

LightGBM(train_lgbm.py)은 사람이 미리 계산한 힌트 컬럼(lag, 기상, 달력 등)을
보고 배우는 방식이라면, LSTM은 "최근 24시간 값의 흐름"을 순서대로 읽어서
다음 1시간을 예측하는 방식이다.

다변량(multivariate): 태양광/풍력은 발전량과 함께 그 시점의 기상(지점 평균 일사량/풍속)도
시퀀스에 넣는다. 수요는 단변량. (입력 차원을 작게 유지하려고 지점 평균 하나만 사용)

공정한 비교를 위해 LightGBM과 "똑같은" 구간·방식으로 평가한다:

    |------------- train -------------|---- calib (4주) ----|-- test (2주) --|
     1차 학습                            PI margin 계산          최종 평가만
     |----------- 2차 학습 (train + calib) -------------------|

- 예측: 1시간씩 예측하고 그 예측값을 다음 입력으로 다시 넣어 24h/168h를 이어서 예측(recursive).
        미래 기상은 LightGBM과 같이 관측값을 예보 대용으로 쓴다.
- PI  : calib 구간에서 168시간 이어서 예측한 오차로 "몇 시간 뒤"별 90% margin (conformal)
- 평가: test 구간에서 매일 0시에 시작한 24h/168h 예측의 MAPE / nMAE(설비용량 기준) / PI 커버리지

결과 파일:
    saved/{target}_lstm.pt        : 최종 LSTM 모델
    saved/{target}_lstm_eval.json : LSTM test 성적
    saved/model_comparison.md     : LightGBM vs LSTM 비교표
                                    (LightGBM 성적은 saved/{target}_pi.json에서 읽음)

실행 (LightGBM 먼저):
    python3 models/train_lgbm.py
    python3 models/train_lstm.py
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from torch import nn

sys.path.append(str(Path(__file__).resolve().parents[1]))

from data_client import available_range, get_mock_data
from metrics import mape, nmae
from models.train_lgbm import (
    CALIB_DAYS,
    CALIB_ORIGIN_STEP_H,
    CONFIDENCE,
    MAX_HORIZON,
    METRIC_NAME,
    TEST_DAYS,
    _passes,
    _report,
)
from pi import compute_horizon_margins

SAVED_DIR = Path(__file__).resolve().parent / "saved"
SAVED_DIR.mkdir(exist_ok=True)

TARGET_COLUMN = {"demand": "demand_mw", "solar": "solar_mw", "wind": "wind_mw"}
# target 자신 외에 같이 넣어줄 날씨 covariate. 없으면(demand) 단변량으로 학습.
# LSTM은 입력 차원이 너무 커지지 않게 지점 "평균" 컬럼 하나만 쓴다
# (LightGBM은 지점별 컬럼까지 전부 사용 — features.feature_columns).
WEATHER_COLUMN = {"solar": "irradiance", "wind": "wind_speed"}
LOOKBACK = 24  # 최근 24시간을 보고 다음 1시간을 예측
EPOCHS = 30
SEED = 42

PredictFn = Callable[[np.ndarray], np.ndarray]


def make_sequences(feature_matrix: np.ndarray, lookback: int) -> tuple[np.ndarray, np.ndarray]:
    """(n_timesteps, n_features) 배열을 (과거 lookback개 -> 다음 1개 target) 쌍으로 재구성.
    target(예측 대상)은 항상 feature_matrix의 0번째 열이라고 약속한다."""
    xs, ys = [], []
    for i in range(len(feature_matrix) - lookback):
        xs.append(feature_matrix[i:i + lookback])
        ys.append(feature_matrix[i + lookback, 0])  # 0번째 열 = target
    return np.array(xs), np.array(ys)


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


def _fit_stats(df: pd.DataFrame, target_col: str, weather_col: str | None) -> dict:
    """정규화 통계 (학습 구간에서만 계산 → test 정보 누출 방지)."""
    stats = {"target_mean": float(df[target_col].mean()), "target_std": float(df[target_col].std())}
    if weather_col is not None:
        stats["weather_mean"] = float(df[weather_col].mean())
        stats["weather_std"] = float(df[weather_col].std())
    return stats


def _build_feature_matrix(df: pd.DataFrame, target_col: str, weather_col: str | None,
                          stats: dict) -> np.ndarray:
    """target(+weather) 컬럼들을 각각 정규화해서 (n, n_features) 배열로 합친다."""
    target_vals = df[target_col].to_numpy(dtype=float)
    target_norm = (target_vals - stats["target_mean"]) / stats["target_std"]
    if weather_col is None:
        return target_norm.reshape(-1, 1)
    weather_vals = df[weather_col].to_numpy(dtype=float)
    weather_norm = (weather_vals - stats["weather_mean"]) / stats["weather_std"]
    return np.stack([target_norm, weather_norm], axis=-1)


def _train(df: pd.DataFrame, target_col: str, weather_col: str | None,
           epochs: int = EPOCHS) -> tuple[SimpleLSTM, dict, float]:
    """df로 LSTM 학습. 반환: (모델, 정규화 통계, 학습시간 초)"""
    torch.manual_seed(SEED)
    stats = _fit_stats(df, target_col, weather_col)
    x, y = make_sequences(_build_feature_matrix(df, target_col, weather_col, stats), LOOKBACK)
    x_t = torch.tensor(x, dtype=torch.float32)
    y_t = torch.tensor(y, dtype=torch.float32)

    model = SimpleLSTM(input_size=x.shape[-1])
    optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
    loss_fn = nn.MSELoss()

    t0 = time.time()
    model.train()
    for epoch in range(epochs):
        optimizer.zero_grad()
        loss = loss_fn(model(x_t), y_t)
        loss.backward()
        optimizer.step()
        if (epoch + 1) % 10 == 0:
            print(f"  epoch {epoch + 1}/{epochs} - loss {loss.item():.4f}")
    model.eval()
    return model, stats, time.time() - t0


def _predict_fn(model: SimpleLSTM) -> PredictFn:
    """(batch, lookback, n_features) numpy → (batch,) 정규화된 예측값."""
    def predict(x: np.ndarray) -> np.ndarray:
        with torch.no_grad():
            return model(torch.tensor(x, dtype=torch.float32)).numpy()
    return predict


def recursive_backtest(predict: PredictFn, raw: pd.DataFrame, target_col: str,
                       weather_col: str | None, stats: dict,
                       origins: list[pd.Timestamp], horizon: int) -> dict[str, np.ndarray]:
    """여러 시작 시점(origin)에서 horizon시간을 이어서 예측 (모든 origin을 한 batch로 처리).

    origin 직전 LOOKBACK시간은 실제값을 넣고, 그 뒤로는 예측값을 다음 입력으로 다시 넣는다.
    미래 기상은 해당 시각의 관측값(예보 대용) — LightGBM과 같은 가정.
    반환: pred / actual (/ capacity) — (n_origins, horizon), MW 단위
    """
    raw = raw.reset_index(drop=True)
    matrix = _build_feature_matrix(raw, target_col, weather_col, stats)
    pos = {ts: i for i, ts in enumerate(raw["datetime"])}
    starts = np.array([pos[o] for o in origins])

    x = np.stack([matrix[p - LOOKBACK:p] for p in starts])  # (b, L, f)
    preds = []
    for h in range(horizon):
        y_norm = predict(x)
        preds.append(y_norm)
        if weather_col is None:
            new_row = y_norm[:, None]
        else:
            new_row = np.stack([y_norm, matrix[starts + h, 1]], axis=-1)
        x = np.concatenate([x[:, 1:, :], new_row[:, None, :]], axis=1)

    pred = np.stack(preds, axis=1) * stats["target_std"] + stats["target_mean"]
    out = {
        "pred": np.clip(pred, 0.0, None),
        "actual": np.stack([raw[target_col].to_numpy()[p:p + horizon] for p in starts]),
    }
    if "capacity_mw" in raw:
        out["capacity"] = np.stack([raw["capacity_mw"].to_numpy()[p:p + horizon] for p in starts])
    return out


def train_one(target: str, start: str | None = None, end: str | None = None) -> dict:
    # 기간을 안 주면 실제 CSV에 들어있는 전체 기간으로 학습
    if start is None or end is None:
        start, end = available_range(target)

    weather_col = WEATHER_COLUMN.get(target)
    target_col = TARGET_COLUMN[target]
    print("=" * 60)
    print(f"target = {target}  (다변량: {weather_col if weather_col else '아니오, 단변량'})")
    print("=" * 60)

    raw = get_mock_data(target, start=start, end=end).sort_values("datetime").reset_index(drop=True)

    # train / calib / test — LightGBM(train_lgbm.py)과 같은 규칙
    data_end = raw["datetime"].max()
    test_start = (data_end - pd.Timedelta(days=TEST_DAYS)).ceil("D")
    calib_start = test_start - pd.Timedelta(days=CALIB_DAYS)

    # 1차 학습(train) → calib 구간에서 168h 이어서 예측 → PI margin
    print("[1차 학습: train]")
    calib_train = raw[raw["datetime"] < calib_start]
    calib_model, calib_stats, _ = _train(calib_train, target_col, weather_col)
    calib_origins = list(pd.date_range(calib_start, test_start - pd.Timedelta(hours=MAX_HORIZON),
                                       freq=f"{CALIB_ORIGIN_STEP_H}h"))
    cb = recursive_backtest(_predict_fn(calib_model), raw, target_col, weather_col, calib_stats,
                            calib_origins, MAX_HORIZON)
    margins = compute_horizon_margins(np.abs(cb["actual"] - cb["pred"]), CONFIDENCE)
    print(f"PI margin(몇 시간 뒤): 1h {margins[0]:.0f} / 24h {margins[23]:.0f} / "
          f"168h {margins[-1]:.0f} MW")

    # 2차 학습(train + calib) → 최종 모델
    print("[2차 학습: train + calib]")
    model, stats, train_seconds = _train(raw[raw["datetime"] < test_start], target_col, weather_col)
    predict = _predict_fn(model)

    # (참고) 1시간 앞 예측 — 실제 과거값을 넣어주므로 낙관적인 수치
    test_raw = raw[raw["datetime"] >= test_start]
    one = recursive_backtest(predict, raw, target_col, weather_col, stats,
                             list(test_raw["datetime"]), 1)
    if target == "demand":
        one_step = mape(one["actual"].ravel(), one["pred"].ravel())
    else:
        one_step = nmae(one["actual"].ravel(), one["pred"].ravel(), one["capacity"].ravel())
    print(f"[참고] 1시간 앞 예측 {METRIC_NAME[target]}: {one_step:.2f}%")

    # test: 매일 0시에 시작 → 24h / 168h (LightGBM과 같은 시점)
    pi_info = {"pi_type": "horizon", "margins": margins}
    test_metrics = {}
    for hz in (24, 168):
        last_origin = data_end - pd.Timedelta(hours=hz) + pd.Timedelta(hours=1)
        origins = list(pd.date_range(test_start, last_origin, freq="D"))
        tb = recursive_backtest(predict, raw, target_col, weather_col, stats, origins, hz)
        dts = [pd.date_range(o, periods=hz, freq="h") for o in origins]
        rep = _report(target, tb, pi_info, dts)
        test_metrics[f"{hz}h"] = rep
        ok = "통과" if _passes(target, f"{hz}h", rep) else "미달"
        print(f"[test {hz:3d}h] {METRIC_NAME[target]} {rep['primary_value']:.2f}% / "
              f"PI 커버리지 {rep['coverage']:.1f}% / "
              f"평균 구간폭 {rep['interval_width_mean']:.0f} MW "
              f"(시점 {len(origins)}개) → {ok}")

    torch.save({
        "state_dict": model.state_dict(),
        "input_size": 1 if weather_col is None else 2,
        "lookback": LOOKBACK,
        "weather_col": weather_col,
        "stats": stats,
    }, SAVED_DIR / f"{target}_lstm.pt")
    evaluation = {
        "model": "lstm",
        "train_seconds": round(train_seconds, 1),
        "test_period": [str(test_start), str(data_end)],
        "one_step": one_step,
        "margins": [round(float(x), 3) for x in margins],
        "test_metrics": test_metrics,
    }
    (SAVED_DIR / f"{target}_lstm_eval.json").write_text(
        json.dumps(evaluation, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"모델/평가 저장 완료: saved/{target}_lstm.pt, saved/{target}_lstm_eval.json\n")
    return evaluation


def write_comparison(lstm_results: dict[str, dict]) -> str:
    """LightGBM(saved/{t}_pi.json) vs LSTM 비교표를 만들어 saved/model_comparison.md로 저장."""
    lines = ["# 예측 모델 비교: LightGBM vs LSTM", ""]
    lines.append("test 구간에서 매일 0시에 시작해 24h/168h를 이어서 예측한 결과 "
                 "(학습·PI 계산에 쓰지 않은 구간, 두 모델 같은 시점).")
    lines.append("수요=MAPE, 태양광/풍력=nMAE(설비용량 기준). PI 커버리지 목표 ≥ 85%.")
    lines.append("")
    lines.append("| 대상 | 모델 | 오차(24h) | 오차(168h) | PI 커버리지(24h) | PI 커버리지(168h) "
                 "| 학습시간 | 기준 통과 |")
    lines.append("|---|---|---|---|---|---|---|---|")

    winners = {}
    for t, lstm in lstm_results.items():
        lgbm_path = SAVED_DIR / f"{t}_pi.json"
        if not lgbm_path.exists():
            lines.append(f"| {t} | LightGBM | (saved/{t}_pi.json 없음 — "
                         f"train_lgbm.py 먼저 실행) |||||||")
            continue
        lgbm = json.loads(lgbm_path.read_text(encoding="utf-8"))
        if lgbm.get("test_period") != lstm["test_period"]:
            print(f"[경고] {t}: LightGBM과 LSTM의 test 구간이 다름 "
                  "→ train_lgbm.py를 다시 실행하세요")

        scores = {}
        for name, res in (("LightGBM", lgbm), ("LSTM", lstm)):
            m24, m168 = res["test_metrics"]["24h"], res["test_metrics"]["168h"]
            ok = _passes(t, "24h", m24) and _passes(t, "168h", m168)
            secs = res.get("train_seconds")
            lines.append(
                f"| {t} | {name} | {m24['primary_value']:.2f}% | {m168['primary_value']:.2f}% "
                f"| {m24['coverage']:.1f}% | {m168['coverage']:.1f}% "
                f"| {f'{secs:.1f}초' if secs is not None else '-'} | {'✅' if ok else '❌'} |")
            scores[name] = (m24["primary_value"] + m168["primary_value"]) / 2
        winners[t] = min(scores, key=scores.get)

    lines.append("")
    lines.append("대상별 더 정확한 모델(24h·168h 오차 평균 기준): "
                 + ", ".join(f"{t}={w}" for t, w in winners.items()))
    text = "\n".join(lines) + "\n"
    (SAVED_DIR / "model_comparison.md").write_text(text, encoding="utf-8")
    return text


if __name__ == "__main__":
    results = {t: train_one(t) for t in ["demand", "solar", "wind"]}
    print("=" * 60)
    print(write_comparison(results))
    print("비교표 저장 완료: saved/model_comparison.md")
