"""
LSTM 학습 + LightGBM과 비교 스크립트 (오프라인 실행 전용, torch 필요)

LightGBM(train_lgbm.py)은 한 시점의 피처 표를 보고 예측하고,
LSTM은 "최근 24시간의 흐름(시퀀스)"을 순서대로 읽어서 다음 1시간을 예측한다.

입력 정보는 LightGBM과 같게 맞춘다 (공정한 비교):
- 수요   : 과거 수요 + 기온·냉방도일(CDD)·난방도일(HDD)·냉난방 도시(CDH/HDH) + 시각·요일·주말·공휴일
- 태양광 : 과거 이용률 + 7개 지점 일사량(+이동평균, 최대, 편차) + 달력
- 풍력   : 과거 발전량 + 6개 지점 풍속(+이동평균, 최대, 편차) + 달력
  (달력·기상 컬럼 목록은 features.feature_columns 와 같고, LightGBM의 lag 컬럼 대신
   LSTM은 시퀀스 안에 "1시간 전 값"을 넣는다. 시각·날짜는 sin/cos 주기 인코딩도 추가)
- 정답 단위도 LightGBM과 같다: 태양광은 이용률(발전량/설비용량), 수요·풍력은 MW.

시퀀스 한 줄(시각 i) = [i-1시의 정답값, i시의 달력·기상] → i시의 정답을 예측.
미래로 이어서 예측할 때는 [방금 예측한 값, 다음 시각의 달력·기상]을 붙여 나간다.
미래 기상은 LightGBM과 같이 관측값을 예보 대용으로 쓴다.

평가는 LightGBM과 "똑같은" 구간·방식:

    |------------- train -------------|---- calib (4주) ----|-- test (2주) --|
     1차 학습                            PI margin 계산          최종 평가만
     |----------- 2차 학습 (train + calib) -------------------|

- PI  : calib 구간에서 168시간 이어서 예측한 오차로 "몇 시간 뒤"별 90% margin (conformal)
- 평가: test 구간에서 매일 0시에 시작한 24h/168h 예측의 MAPE / nMAE(설비용량 기준) / PI 커버리지

하이퍼파라미터는 test 결과를 보고 고른 값이 아니라 일반적인 기본값이다
(hidden 64, mini-batch 256, Adam lr 1e-3, 15 epoch).

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

# forecast_api/ 와 models/ 를 import 경로에 추가.
# models 는 패키지 이름으로 부르지 않는다
# (dispatch_api 에도 models.py 가 있어서 테스트에서 이름이 겹친다).
sys.path.append(str(Path(__file__).resolve().parents[1]))
sys.path.append(str(Path(__file__).resolve().parent))

from data_client import available_range, get_mock_data
from features import TARGET_COLUMN, build_exog_features, feature_columns
from forecast_engine import TARGET_MODE
from metrics import mape, nmae
from pi import compute_horizon_margins
from train_lgbm import (
    CALIB_DAYS,
    CALIB_ORIGIN_STEP_H,
    CONFIDENCE,
    MAX_HORIZON,
    METRIC_NAME,
    TEST_DAYS,
    _passes,
    _report,
)

SAVED_DIR = Path(__file__).resolve().parent / "saved"
SAVED_DIR.mkdir(exist_ok=True)

LOOKBACK = 24      # 최근 24시간을 보고 다음 1시간을 예측
HIDDEN_SIZE = 64
EPOCHS = 15
BATCH_SIZE = 256
LEARNING_RATE = 1e-3
SEED = 42

CYCLIC_COLUMNS = ["hour_sin", "hour_cos", "doy_sin", "doy_cos"]

PredictFn = Callable[[np.ndarray], np.ndarray]


def lstm_exog_columns(target: str) -> list[str]:
    """LSTM에 넣는 달력·기상 컬럼 = LightGBM 피처에서 정답 lag 컬럼만 뺀 것 + 주기 인코딩."""
    target_col = TARGET_COLUMN[target]
    base = [c for c in feature_columns(target) if not c.startswith(target_col)]
    return base + CYCLIC_COLUMNS


def add_cyclic_features(df: pd.DataFrame) -> pd.DataFrame:
    """시각·날짜를 sin/cos로 바꾼 컬럼 추가 (23시와 0시가 가깝다는 걸 모델이 알게)."""
    df = df.copy()
    df["hour_sin"] = np.sin(2 * np.pi * df["hour"] / 24)
    df["hour_cos"] = np.cos(2 * np.pi * df["hour"] / 24)
    df["doy_sin"] = np.sin(2 * np.pi * df["dayofyear"] / 366)
    df["doy_cos"] = np.cos(2 * np.pi * df["dayofyear"] / 366)
    return df


def prepare(raw: pd.DataFrame, target: str) -> pd.DataFrame:
    """raw → 달력·기상 피처 + 주기 인코딩 + 학습 정답 y (MW 또는 이용률)."""
    df = build_exog_features(raw, target)  # 시간순 정렬 + 달력·기상(수요는 기온·CDD/HDD) 피처
    df = add_cyclic_features(df)
    y = df[TARGET_COLUMN[target]].astype(float)
    if TARGET_MODE[target] == "cf":
        y = y / df["capacity_mw"]
    df["y"] = y
    return df


def fit_stats(df: pd.DataFrame, exog_cols: list[str]) -> dict:
    """정규화 통계 (학습 구간에서만 계산 → test 정보 누출 방지)."""
    std = df[exog_cols].std().replace(0, 1.0).fillna(1.0)
    return {
        "y_mean": float(df["y"].mean()),
        "y_std": float(df["y"].std()) or 1.0,
        "exog_cols": list(exog_cols),
        "exog_mean": df[exog_cols].mean().tolist(),
        "exog_std": std.tolist(),
    }


def build_matrix(df: pd.DataFrame, stats: dict) -> tuple[np.ndarray, np.ndarray]:
    """(n, 1 + n_exog) 입력 행렬과 정규화된 정답(n,)을 만든다.

    i번째 행 = [i-1시의 정답(정규화), i시의 달력·기상(정규화)]  → 0번째 행의 첫 칸은 NaN
    """
    y_norm = (df["y"].to_numpy(dtype=float) - stats["y_mean"]) / stats["y_std"]
    exog = df[stats["exog_cols"]].to_numpy(dtype=float)
    exog = (exog - np.array(stats["exog_mean"])) / np.array(stats["exog_std"])
    lag = np.r_[np.nan, y_norm[:-1]]
    return np.column_stack([lag, exog]), y_norm


def make_sequences(matrix: np.ndarray, y_norm: np.ndarray,
                   lookback: int) -> tuple[np.ndarray, np.ndarray]:
    """끝 행이 e인 길이 lookback 시퀀스 → 정답 y[e]. (0번째 행은 lag가 없어서 e ≥ lookback)"""
    xs, ys = [], []
    for e in range(lookback, len(matrix)):
        xs.append(matrix[e - lookback + 1:e + 1])
        ys.append(y_norm[e])
    return np.array(xs, dtype=np.float32), np.array(ys, dtype=np.float32)


class SimpleLSTM(nn.Module):
    def __init__(self, input_size: int = 1, hidden_size: int = HIDDEN_SIZE):
        super().__init__()
        self.lstm = nn.LSTM(input_size=input_size, hidden_size=hidden_size, batch_first=True)
        self.head = nn.Linear(hidden_size, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (batch, lookback, input_size)
        out, _ = self.lstm(x)
        last_step = out[:, -1, :]  # 마지막 시점의 은닉 상태만 사용
        return self.head(last_step).squeeze(-1)


def train_model(df: pd.DataFrame, exog_cols: list[str],
                epochs: int = EPOCHS) -> tuple[SimpleLSTM, dict, float]:
    """df로 LSTM 학습 (mini-batch). 반환: (모델, 정규화 통계, 학습시간 초)"""
    torch.manual_seed(SEED)
    stats = fit_stats(df, exog_cols)
    matrix, y_norm = build_matrix(df, stats)
    xs, ys = make_sequences(matrix, y_norm, LOOKBACK)
    x_t, y_t = torch.from_numpy(xs), torch.from_numpy(ys)

    model = SimpleLSTM(input_size=xs.shape[-1])
    optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)
    loss_fn = nn.MSELoss()

    t0 = time.time()
    model.train()
    for epoch in range(epochs):
        perm = torch.randperm(len(x_t))
        total = 0.0
        for i in range(0, len(perm), BATCH_SIZE):
            idx = perm[i:i + BATCH_SIZE]
            optimizer.zero_grad()
            loss = loss_fn(model(x_t[idx]), y_t[idx])
            loss.backward()
            optimizer.step()
            total += loss.item() * len(idx)
        if (epoch + 1) % 5 == 0:
            print(f"  epoch {epoch + 1}/{epochs} - loss {total / len(x_t):.4f}")
    model.eval()
    return model, stats, time.time() - t0


def predict_fn(model: SimpleLSTM) -> PredictFn:
    """(batch, lookback, n_features) numpy → (batch,) 정규화된 예측값."""
    def predict(x: np.ndarray) -> np.ndarray:
        with torch.no_grad():
            return model(torch.tensor(x, dtype=torch.float32)).numpy()
    return predict


def recursive_backtest(predict: PredictFn, df: pd.DataFrame, target: str, stats: dict,
                       origins: list[pd.Timestamp], horizon: int) -> dict[str, np.ndarray]:
    """여러 시작 시점(origin)에서 horizon시간을 이어서 예측 (모든 origin을 한 batch로 처리).

    origin 시각 p의 첫 입력 = [p-24 ~ p 행] (마지막 행 = [p-1시 실제값, p시 달력·기상]).
    그 뒤로는 [방금 예측한 값, 다음 시각 달력·기상] 행을 붙여 나간다.
    반환: pred / actual (/ capacity) — (n_origins, horizon), MW 단위
    """
    df = df.reset_index(drop=True)
    matrix, _ = build_matrix(df, stats)
    pos = {ts: i for i, ts in enumerate(df["datetime"])}
    starts = np.array([pos[o] for o in origins])

    x = np.stack([matrix[p - LOOKBACK + 1:p + 1] for p in starts])  # (b, L, f)
    preds = []
    for h in range(horizon):
        y_norm = predict(x)
        preds.append(y_norm)
        if h + 1 < horizon:
            nxt = matrix[starts + h + 1].copy()
            nxt[:, 0] = y_norm                       # 1시간 전 값 자리에 예측값을 넣는다
            x = np.concatenate([x[:, 1:, :], nxt[:, None, :]], axis=1)

    pred = np.stack(preds, axis=1) * stats["y_std"] + stats["y_mean"]
    if TARGET_MODE[target] == "cf":
        cap_at_origin = df["capacity_mw"].to_numpy()[starts - 1]  # 예측 시점의 최신 설비용량
        pred = pred * cap_at_origin[:, None]

    target_vals = df[TARGET_COLUMN[target]].to_numpy()
    out = {
        "pred": np.clip(pred, 0.0, None),
        "actual": np.stack([target_vals[p:p + horizon] for p in starts]),
    }
    if "capacity_mw" in df:
        cap = df["capacity_mw"].to_numpy()
        out["capacity"] = np.stack([cap[p:p + horizon] for p in starts])
    return out


def train_one(target: str, start: str | None = None, end: str | None = None) -> dict:
    # 기간을 안 주면 실제 CSV에 들어있는 전체 기간으로 학습
    if start is None or end is None:
        start, end = available_range(target)

    exog_cols = lstm_exog_columns(target)
    print("=" * 60)
    print(f"target = {target}  (입력 {1 + len(exog_cols)}개: 1시간 전 값 + 달력·기상 "
          f"{len(exog_cols)}개, 정답: {TARGET_MODE[target]})")
    print("=" * 60)

    raw = get_mock_data(target, start=start, end=end).sort_values("datetime").reset_index(drop=True)
    df = prepare(raw, target)

    # train / calib / test — LightGBM(train_lgbm.py)과 같은 규칙
    data_end = df["datetime"].max()
    test_start = (data_end - pd.Timedelta(days=TEST_DAYS)).ceil("D")
    calib_start = test_start - pd.Timedelta(days=CALIB_DAYS)

    # 1차 학습(train) → calib 구간에서 168h 이어서 예측 → PI margin
    print("[1차 학습: train]")
    calib_model, calib_stats, _ = train_model(df[df["datetime"] < calib_start], exog_cols)
    calib_origins = list(pd.date_range(calib_start, test_start - pd.Timedelta(hours=MAX_HORIZON),
                                       freq=f"{CALIB_ORIGIN_STEP_H}h"))
    cb = recursive_backtest(predict_fn(calib_model), df, target, calib_stats,
                            calib_origins, MAX_HORIZON)
    margins = compute_horizon_margins(np.abs(cb["actual"] - cb["pred"]), CONFIDENCE)
    print(f"PI margin(몇 시간 뒤): 1h {margins[0]:.0f} / 24h {margins[23]:.0f} / "
          f"168h {margins[-1]:.0f} MW")

    # 2차 학습(train + calib) → 최종 모델
    print("[2차 학습: train + calib]")
    model, stats, train_seconds = train_model(df[df["datetime"] < test_start], exog_cols)
    predict = predict_fn(model)

    # (참고) 1시간 앞 예측 — 실제 과거값을 넣어주므로 낙관적인 수치
    test_times = list(df.loc[df["datetime"] >= test_start, "datetime"])
    one = recursive_backtest(predict, df, target, stats, test_times, 1)
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
        tb = recursive_backtest(predict, df, target, stats, origins, hz)
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
        "input_size": 1 + len(exog_cols),
        "hidden_size": HIDDEN_SIZE,
        "lookback": LOOKBACK,
        "target_mode": TARGET_MODE[target],
        "stats": stats,
    }, SAVED_DIR / f"{target}_lstm.pt")
    evaluation = {
        "model": "lstm",
        "inputs": ["prev_target", *exog_cols],
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
    lines.append("두 모델 모두 같은 달력·기상 정보(수요: 기온·냉난방도일·일사량·공휴일 / "
                 "태양광·풍력: 지점별 일사량·풍속)를 쓴다. "
                 "LightGBM은 과거값을 lag 피처로, LSTM은 최근 24시간 시퀀스로 본다.")
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

        scores, passed = {}, {}
        for name, res in (("LightGBM", lgbm), ("LSTM", lstm)):
            m24, m168 = res["test_metrics"]["24h"], res["test_metrics"]["168h"]
            ok = _passes(t, "24h", m24) and _passes(t, "168h", m168)
            secs = res.get("train_seconds")
            lines.append(
                f"| {t} | {name} | {m24['primary_value']:.2f}% | {m168['primary_value']:.2f}% "
                f"| {m24['coverage']:.1f}% | {m168['coverage']:.1f}% "
                f"| {f'{secs:.1f}초' if secs is not None else '-'} | {'✅' if ok else '❌'} |")
            scores[name] = (m24["primary_value"] + m168["primary_value"]) / 2
            passed[name] = ok
        # 기준을 통과한 모델 중에서 더 정확한 쪽 (둘 다 미달이면 그냥 더 정확한 쪽)
        pool = [n for n in scores if passed[n]] or list(scores)
        winners[t] = min(pool, key=scores.get)

    lines.append("")
    lines.append("대상별 서빙 모델(기준 통과 모델 중 24h·168h 오차 평균이 낮은 쪽): "
                 + ", ".join(f"{t}={w}" for t, w in winners.items()))
    text = "\n".join(lines) + "\n"
    (SAVED_DIR / "model_comparison.md").write_text(text, encoding="utf-8")
    return text


if __name__ == "__main__":
    results = {t: train_one(t) for t in ["demand", "solar", "wind"]}
    print("=" * 60)
    print(write_comparison(results))
    print("비교표 저장 완료: saved/model_comparison.md")
