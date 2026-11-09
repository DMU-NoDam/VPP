"""
예측 엔진 — data_client / features / models / metrics / pi 를 조립해서
"미래 horizon_hours 시간의 예측값 + 90% 구간"을 만들어내는 핵심 로직.

router.py는 이 모듈을 호출하기만 하고, HTTP 관련 코드는 여기 두지 않는다.

예측 방식 (target별):
- demand: recursive — 1시간씩 예측하고, 그 예측값을 다음 시간의 lag로 다시 넣는다.
          피처 = 과거 수요(lag) + 기온·냉난방도일 + 달력(공휴일 포함)
- solar : direct — 기상 + 달력만으로 각 시각을 바로 예측. 정답은 "이용률(발전량/설비용량)"로
          학습하고, 마지막에 설비용량을 곱한다 (태양광 설비가 계속 늘어나는 걸 반영).
- wind  : direct — 기상 + 달력만으로 각 시각의 발전량(MW)을 바로 예측.

서빙 모델 (model="auto"일 때, models/saved/model_comparison.md 비교 결과로 정함):
- demand: LSTM, solar: LightGBM, wind: LSTM
  LSTM 파일/torch가 없으면 자동으로 LightGBM으로 대체한다.

미래 기상(기온/일사량/풍속)은 원래 기상청 "예보"를 써야 하지만, 예보 API가 아직 없어서
data_client.get_weather로 해당 시각의 "관측값"을 예보값 대용으로 쓴다.
관측값도 없는 시각(데이터 끝 이후)은 과거 데이터의 "시간대별 평균"으로 채운다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from data_client import get_mock_data, get_weather, weather_columns
from features import TARGET_COLUMN, build_exog_features, build_features, feature_columns
from metrics import evaluate
from pi import apply_horizon_pi, apply_hourly_pi, fit_conformal

MODELS_DIR = Path(__file__).resolve().parent / "models"
SAVED_DIR = MODELS_DIR / "saved"

STRATEGY = {"demand": "recursive", "solar": "direct", "wind": "direct"}
TARGET_MODE = {"demand": "mw", "solar": "cf", "wind": "mw"}  # cf = 이용률(발전량/설비용량)

# 미래 구간 기상 피처(가운데 정렬 이동평균)를 계산할 때 앞뒤로 붙이는 여유 시간
_CONTEXT_BEFORE_H = 24
_CONTEXT_AFTER_H = 12

# model="auto"일 때 target별로 쓰는 모델 (같은 test 구간에서 더 정확했던 쪽)
SERVING_MODEL = {"demand": "lstm", "solar": "lightgbm", "wind": "lstm"}

# 하위 호환: 예전 코드에서 쓰던 이름
FEATURE_COLUMNS = {t: feature_columns(t) for t in TARGET_COLUMN}


class ModelLoadError(Exception):
    """모델 파일은 있지만 읽을 수 없을 때 (손상, 형식 불일치 등)."""


def _load_model(target: str, model_name: str = "lightgbm") -> tuple[lgb.Booster, str]:
    """LightGBM 모델을 읽는다. (LSTM은 _load_lstm)"""
    lgbm_path = SAVED_DIR / f"{target}_lgbm.txt"
    if not lgbm_path.exists():
        raise FileNotFoundError(f"{lgbm_path} 없음 — models/train_lgbm.py 먼저 실행 필요")
    return lgb.Booster(model_file=str(lgbm_path)), "lightgbm"


def _load_lstm(target: str):
    """LSTM 모델 + 정규화 통계 + 평가 결과(PI margin, test 성적)를 읽는다.

    반환: (train_lstm 모듈, 모델, 정규화 통계, 평가 dict)
    - 파일 없음 → FileNotFoundError / torch 미설치 → NotImplementedError / 손상 → ModelLoadError
    """
    pt_path = SAVED_DIR / f"{target}_lstm.pt"
    eval_path = SAVED_DIR / f"{target}_lstm_eval.json"
    if not pt_path.exists() or not eval_path.exists():
        raise FileNotFoundError(f"{pt_path.name} / {eval_path.name} 없음 — "
                                "models/train_lstm.py 먼저 실행 필요")
    try:
        import torch

        # torch가 필요해서 LSTM을 쓸 때만 불러온다. "models" 패키지 이름은 dispatch_api/models.py와
        # 겹칠 수 있어서, models/ 폴더를 경로에 넣고 train_lstm 모듈 이름으로 직접 부른다.
        if str(MODELS_DIR) not in sys.path:
            sys.path.insert(0, str(MODELS_DIR))
        import train_lstm
    except ImportError as e:
        raise NotImplementedError("LSTM 예측에는 torch 설치가 필요합니다") from e

    try:
        ckpt = torch.load(pt_path, map_location="cpu")
        model = train_lstm.SimpleLSTM(input_size=ckpt["input_size"],
                                      hidden_size=ckpt.get("hidden_size", train_lstm.HIDDEN_SIZE))
        model.load_state_dict(ckpt["state_dict"])
        model.eval()
        evaluation = json.loads(eval_path.read_text(encoding="utf-8"))
        return train_lstm, model, ckpt["stats"], evaluation
    except Exception as e:  # 예전 형식(.pt)이거나 손상된 파일
        raise ModelLoadError(f"{pt_path.name} 로드 실패: {e}") from e


def _future_exog(target: str, history: pd.DataFrame, horizon_hours: int) -> pd.DataFrame:
    """미래 horizon_hours 시간의 달력·기상 피처 표 (lag 제외).

    기상값 우선순위: (1) 그 시각의 관측값(예보 대용) → (2) history에 있는 값
                    → (3) history의 시간대별 평균
    """
    cols = weather_columns(target)
    future_start = history["datetime"].max() + pd.Timedelta(hours=1)
    future_idx = pd.date_range(future_start, periods=horizon_hours, freq="h")
    ctx_idx = pd.date_range(future_start - pd.Timedelta(hours=_CONTEXT_BEFORE_H),
                            future_idx[-1] + pd.Timedelta(hours=_CONTEXT_AFTER_H), freq="h")

    observed = get_weather(target, start=str(ctx_idx[0]), end=str(ctx_idx[-1]))
    frame = observed.reindex(ctx_idx)
    past = history.set_index("datetime")[cols]
    frame = frame.fillna(past.reindex(ctx_idx))
    profile = history.groupby(history["datetime"].dt.hour)[cols].mean()
    for c in cols:
        missing = frame[c].isna()
        frame.loc[missing, c] = profile[c].reindex(frame.index[missing].hour).to_numpy()
    frame = frame.fillna(0.0)

    frame = frame.rename_axis("datetime").reset_index()
    exog = build_exog_features(frame, target)
    return exog[exog["datetime"].isin(future_idx)].reset_index(drop=True)


def _recursive_forecast(target: str, model, history: pd.DataFrame,
                        horizon_hours: int) -> pd.DataFrame:
    """1시간씩 순차적으로 미래를 예측하면서, 예측값을 다음 스텝의 lag로 재사용한다.
    history: 과거 168시간 이상 (datetime, 정답 컬럼, 기상 컬럼)"""
    target_col = TARGET_COLUMN[target]
    feature_cols = feature_columns(target)
    exog = _future_exog(target, history, horizon_hours)
    values: dict[pd.Timestamp, float] = dict(zip(history["datetime"], history[target_col]))

    rows = []
    for rec in exog.to_dict("records"):
        ts = rec["datetime"]
        rec[f"{target_col}_lag_1h"] = values[ts - pd.Timedelta(hours=1)]
        rec[f"{target_col}_lag_24h"] = values[ts - pd.Timedelta(hours=24)]
        rec[f"{target_col}_lag_168h"] = values[ts - pd.Timedelta(hours=168)]
        recent_24 = [values[ts - pd.Timedelta(hours=h)] for h in range(1, 25)]
        rec[f"{target_col}_roll_mean_24h"] = float(np.mean(recent_24))

        # DataFrame을 매 스텝 만들면 느려서(백테스트에서 수만 번 호출) numpy 1행으로 넘긴다.
        x_row = np.array([[rec[c] for c in feature_cols]], dtype=float)
        pred = max(float(model.predict(x_row)[0]), 0.0)  # 수요/발전량은 음수가 될 수 없음
        values[ts] = pred
        rows.append({"datetime": ts, "predicted": pred})
    return pd.DataFrame(rows)


def _direct_forecast(target: str, model, history: pd.DataFrame,
                     horizon_hours: int) -> pd.DataFrame:
    """기상 + 달력만으로 각 시각을 한 번에 예측한다 (과거 발전량 안 씀)."""
    exog = _future_exog(target, history, horizon_hours)
    pred = model.predict(exog[feature_columns(target)].to_numpy(dtype=float))
    if TARGET_MODE[target] == "cf":
        capacity = float(history["capacity_mw"].iloc[-1])  # 예측 시점의 최신 설비용량
        pred = pred * capacity
    return pd.DataFrame({"datetime": exog["datetime"], "predicted": np.clip(pred, 0.0, None)})


def forecast_path(target: str, model, history: pd.DataFrame, horizon_hours: int) -> pd.DataFrame:
    """target의 예측 방식(STRATEGY)에 맞게 미래 horizon_hours 시간을 예측한다."""
    if STRATEGY[target] == "recursive":
        return _recursive_forecast(target, model, history, horizon_hours)
    return _direct_forecast(target, model, history, horizon_hours)


def backtest(target: str, model, raw: pd.DataFrame,
             origins: list[pd.Timestamp], horizon_hours: int) -> dict[str, np.ndarray]:
    """여러 시작 시점(origin)에서 API와 똑같은 방식으로 horizon_hours만큼 예측해보고 결과를 모은다.

    origin 이전 데이터만 history로 넘기므로 미래 정답을 볼 수 없다.
    반환: pred / actual / capacity / hour — 모두 (n_origins, H). 실제값이 없는 칸은 NaN
    """
    indexed = raw.set_index("datetime")
    actual = indexed[TARGET_COLUMN[target]]
    capacity = indexed["capacity_mw"] if "capacity_mw" in indexed else None

    out: dict[str, list] = {"pred": [], "actual": [], "capacity": [], "hour": []}
    for origin in origins:
        hist = raw[raw["datetime"] < origin]
        f = forecast_path(target, model, hist, horizon_hours)
        out["pred"].append(f["predicted"].to_numpy())
        out["actual"].append(actual.reindex(f["datetime"]).to_numpy())
        out["hour"].append(f["datetime"].dt.hour.to_numpy())
        if capacity is not None:
            out["capacity"].append(capacity.reindex(f["datetime"]).ffill().bfill().to_numpy())
    result = {k: np.array(v) for k, v in out.items() if v}
    return result


# 하위 호환 이름
recursive_backtest = backtest


def _lstm_forecast(target: str, train_lstm, model, stats: dict, history: pd.DataFrame,
                   horizon_hours: int) -> pd.DataFrame:
    """LSTM으로 미래 horizon_hours 시간을 이어서 예측한다.

    과거 구간(history)과 미래 구간(달력·기상만 있고 정답은 비어 있음)을 이어 붙인 뒤,
    학습 때 평가에 쓴 것과 같은 함수(train_lstm.recursive_backtest)로 예측한다.
    """
    target_col = TARGET_COLUMN[target]
    past = train_lstm.prepare(history, target)
    future = train_lstm.add_cyclic_features(_future_exog(target, history, horizon_hours))
    future["y"] = np.nan
    future[target_col] = np.nan
    if "capacity_mw" in past:
        future["capacity_mw"] = float(past["capacity_mw"].iloc[-1])
    combined = pd.concat([past, future], ignore_index=True)

    out = train_lstm.recursive_backtest(train_lstm.predict_fn(model), combined, target, stats,
                                        [future["datetime"].iloc[0]], horizon_hours)
    return pd.DataFrame({"datetime": future["datetime"], "predicted": out["pred"][0]})


def _load_pi(target: str) -> dict | None:
    """train_lgbm.py가 저장한 PI margin + 시험구간 성적을 읽는다. 없으면 None."""
    path = SAVED_DIR / f"{target}_pi.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def interval(pi_info: dict, preds: np.ndarray,
             datetimes: pd.Series | pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    """pi.json의 margin 종류에 맞게 예측구간을 씌운다.
    horizon: "몇 시간 뒤"별 margin (수요) / hourly: "몇 시"별 margin (태양광·풍력)"""
    if pi_info["pi_type"] == "hourly":
        hours = pd.DatetimeIndex(datetimes).hour
        return apply_hourly_pi(preds, np.array(pi_info["margins"]), hours)
    return apply_horizon_pi(preds, np.array(pi_info["margins"]))


def run_forecast(target: str, start_date: str, end_date: str,
                  horizon_hours: int, model_name: str = "auto",
                  pi_method: str = "conformal") -> dict:
    """단일 target에 대한 예측 + PI + metrics를 전부 만들어서 반환.

    model_name: "auto"면 SERVING_MODEL(target별 더 정확한 모델), "lightgbm"/"lstm"이면 그 모델.
                auto인데 LSTM을 쓸 수 없으면(파일·torch 없음) LightGBM으로 대체한다.
    metrics: 저장된 평가 파일이 있으면 "학습에 안 쓴 시험 구간에서 API와 같은 방식으로 예측한"
             성적(24h 이하 요청이면 24h 성적, 그보다 길면 168h 성적)을 돌려준다.
    """
    raw = get_mock_data(target, start=start_date, end=end_date)
    choice = SERVING_MODEL[target] if model_name == "auto" else model_name

    lstm = None
    if choice == "lstm":
        try:
            lstm = _load_lstm(target)
        except (FileNotFoundError, NotImplementedError, ModelLoadError) as e:
            if model_name != "auto":
                raise
            print(f"[forecast_engine] {target}: LSTM 사용 불가({e}) → LightGBM으로 대체")

    if lstm is not None:
        train_lstm, lstm_model, stats, evaluation = lstm
        forecast_df = _lstm_forecast(target, train_lstm, lstm_model, stats, raw, horizon_hours)
        model_used = "lstm"
        model = None
        pi_info = {"pi_type": "horizon", "margins": evaluation["margins"],
                   "test_metrics": evaluation["test_metrics"]}
    else:
        model, model_used = _load_model(target)
        forecast_df = forecast_path(target, model, raw, horizon_hours)
        pi_info = _load_pi(target)
    preds = forecast_df["predicted"].to_numpy()

    if pi_info is not None and "pi_type" in pi_info:
        lower, upper = interval(pi_info, preds, forecast_df["datetime"])
        key = "24h" if horizon_hours <= 24 else "168h"
        metrics = dict(pi_info["test_metrics"][key])
    else:
        # 폴백(pi.json 없음): 학습 데이터의 마지막 15% 오차로 단일 margin
        target_col = TARGET_COLUMN[target]
        df = build_features(raw, target_col)
        calib = df.iloc[int(len(df) * 0.85):]
        calib_pred = model.predict(calib[feature_columns(target)].to_numpy(dtype=float))
        if TARGET_MODE[target] == "cf":
            calib_pred = calib_pred * calib["capacity_mw"].to_numpy()
        predict_with_pi = fit_conformal(calib[target_col].values, calib_pred, confidence=0.9)
        calib_lower, calib_upper = predict_with_pi(calib_pred)
        capacity = calib["capacity_mw"].to_numpy() if "capacity_mw" in calib else None
        metrics = evaluate(target, calib[target_col].values, calib_pred, calib_lower, calib_upper,
                           capacity=capacity)
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
    result = run_forecast("demand", start_date="2025-07-01", end_date="2026-10-01",
                          horizon_hours=24)
    print(f"model_used: {result['model_used']}")
    print(f"metrics: {result['metrics']}")
    print(f"points[:3]: {json.dumps(result['points'][:3], indent=2, default=str)}")
