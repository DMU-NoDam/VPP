"""forecast_api 단위 테스트.

대상: data_client / features / metrics / pi / forecast_engine / router / 학습 스크립트.

실제 CSV 대신 작은 합성 CSV(35일치)를 임시 폴더에 만들어서 data_client.DATA_DIR 를 바꿔 끼운다.
→ 실데이터가 바뀌어도 테스트 결과가 흔들리지 않고, 빠르게 돈다.
서비스끼리는 import 하지 않으므로 forecast_api 폴더를 sys.path 에 넣고 평평하게 import 한다.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

FORECAST_DIR = Path(__file__).resolve().parents[1] / "services" / "forecast_api"
if str(FORECAST_DIR) not in sys.path:
    sys.path.insert(0, str(FORECAST_DIR))

import data_client  # noqa: E402
import features  # noqa: E402
import forecast_engine  # noqa: E402
import metrics  # noqa: E402
import pi  # noqa: E402
from models import train_lgbm  # noqa: E402

N_DAYS = 35
START = "2026-01-01"


# --- 합성 데이터 ----------------------------------------------------------------


def _write_synthetic_csv(folder: Path) -> None:
    rng = np.random.default_rng(0)
    hours = pd.date_range(START, periods=24 * N_DAYS, freq="h")
    hod = hours.hour.to_numpy()
    n = len(hours)

    demand = 60000 + 8000 * np.sin((hod - 6) / 24 * 2 * np.pi) + rng.normal(0, 500, n)
    pd.DataFrame({"ts": hours.strftime("%Y-%m-%d %H:%M:%S"), "demand_mw": demand.round(1)}) \
        .to_csv(folder / "power_demand.csv", index=False)

    daylight = np.clip(np.sin((hod - 6) / 12 * np.pi), 0, None)
    wind_speed = np.clip(3 + 2 * np.sin(np.arange(n) / 9) + rng.normal(0, 0.3, n), 0, None)
    solar_cap = np.linspace(12000, 12500, n)
    rows = []
    for i, ts in enumerate(hours.strftime("%Y-%m-%d %H:%M:%S")):
        rows.append((ts, "solar", solar_cap[i], solar_cap[i] * 0.6 * daylight[i]))
        rows.append((ts, "wind", 2800.0, 300 + 100 * wind_speed[i]))
        rows.append((ts, "nuclear", 24000.0, 20000.0))
    pd.DataFrame(rows, columns=["ts", "fuel", "capacity_mw", "trade_mwh"]) \
        .to_csv(folder / "trading_by_fuel.csv", index=False)

    stations = sorted(set(data_client.SOLAR_STATIONS) | set(data_client.WIND_STATIONS))
    wrows = []
    for i, ts in enumerate(hours.strftime("%Y-%m-%d %H:%M:%S")):
        for s in stations:
            rad = "None" if daylight[i] == 0 else round(3 * daylight[i], 2)  # 밤 일사량은 결측
            wrows.append((ts, s, round(wind_speed[i], 2), round(5 + 5 * daylight[i], 1), 60.0, rad))
    pd.DataFrame(wrows, columns=["observed_at", "station_id", "wind_speed_ms", "temperature_c",
                                 "humidity_pct", "solar_radiation_mj"]) \
        .to_csv(folder / "weather.csv", index=False)


@pytest.fixture(scope="module", autouse=True)
def synthetic_data(tmp_path_factory):
    folder = tmp_path_factory.mktemp("csv")
    _write_synthetic_csv(folder)
    original = data_client.DATA_DIR
    data_client.DATA_DIR = folder
    yield folder
    data_client.DATA_DIR = original


@pytest.fixture(scope="module")
def raw():
    return {t: data_client.get_data(t, *data_client.available_range(t))
            for t in ("demand", "solar", "wind")}


@pytest.fixture(scope="module")
def boosters(raw):
    return {t: train_lgbm._fit(t, features.build_features(raw[t], features.TARGET_COLUMN[t]))
            for t in raw}


# --- data_client ---------------------------------------------------------------


def test_weather_columns():
    assert data_client.weather_columns("demand") == ["temperature_c"]
    solar_cols = data_client.weather_columns("solar")
    assert len(solar_cols) == len(data_client.SOLAR_STATIONS) + 1 and solar_cols[-1] == "irradiance"
    assert data_client.weather_columns("wind")[-1] == "wind_speed"


def test_get_demand_hourly_with_temperature(raw):
    df = raw["demand"]
    assert list(df.columns) == ["datetime", "demand_mw", "temperature_c"]
    assert len(df) == 24 * N_DAYS
    assert df.isna().sum().sum() == 0


def test_get_solar_fills_night_irradiance_and_mean(raw):
    df = raw["solar"]
    cols = data_client.weather_columns("solar")
    assert {"solar_mw", "capacity_mw", *cols} <= set(df.columns)
    assert df[cols].isna().sum().sum() == 0
    night = df[df["datetime"].dt.hour == 0]
    assert (night["irradiance"] == 0).all()  # 밤 "None" → 0
    np.testing.assert_allclose(df["irradiance"], df[cols[:-1]].mean(axis=1))


def test_get_wind_has_capacity(raw):
    df = raw["wind"]
    assert (df["capacity_mw"] == 2800).all()
    assert (df["wind_mw"] >= 0).all()


def test_available_range():
    start, end = data_client.available_range("solar")
    assert start.startswith(START)
    assert pd.Timestamp(end) == pd.Timestamp(START) + pd.Timedelta(hours=24 * N_DAYS - 1)


def test_fallback_to_synthetic_when_short(capsys):
    df = data_client.get_data("solar", "2026-01-01", "2026-01-03")  # 240시간 미만 → 목업
    assert "목업으로 대체" in capsys.readouterr().out
    assert set(data_client.weather_columns("solar")) <= set(df.columns)
    assert "capacity_mw" in df


def test_get_weather_beyond_data_is_empty():
    w = data_client.get_weather("wind", "2030-01-01", "2030-01-02")
    assert w.empty and list(w.columns) == data_client.weather_columns("wind")


def test_unknown_target_raises():
    with pytest.raises(ValueError):
        data_client.get_data("coal", "2026-01-01", "2026-01-30")


def test_find_data_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FORECAST_DATA_DIR", str(tmp_path))
    assert data_client._find_data_dir() == tmp_path


# --- features ------------------------------------------------------------------


def test_time_features_holiday():
    df = pd.DataFrame({"datetime": pd.to_datetime(["2026-01-01 10:00", "2026-01-02 10:00",
                                                   "2026-01-03 10:00"])})
    out = features.add_time_features(df)
    assert out["is_holiday"].tolist() == [1, 0, 0]   # 1/1 신정
    assert out["is_offday"].tolist() == [1, 0, 1]    # 1/3 토요일


def test_degree_days():
    dt = pd.date_range("2026-07-01", periods=48, freq="h")
    temp = np.r_[np.full(24, 30.0), np.full(24, 10.0)]
    out = features.add_degree_day_features(pd.DataFrame({"datetime": dt, "temperature_c": temp}))
    assert out["cdd"].iloc[0] == pytest.approx(30 - features.CDD_BASE)
    assert out["hdd"].iloc[0] == 0
    assert out["hdd"].iloc[-1] == pytest.approx(features.HDD_BASE - 10)
    assert out["cdh"].iloc[-1] == 0


def test_lag_and_rolling():
    df = pd.DataFrame({"x": np.arange(200, dtype=float)})
    out = features.add_rolling_features(features.add_lag_features(df, "x"), "x")
    assert out["x_lag_1h"].iloc[10] == 9
    assert out["x_lag_168h"].iloc[170] == 2
    assert out["x_roll_mean_24h"].iloc[23] == pytest.approx(11.5)


def test_build_features_demand_drops_lag_rows(raw):
    df = features.build_features(raw["demand"], "demand_mw")
    assert len(df) == 24 * N_DAYS - 168
    assert df[features.feature_columns("demand")].isna().sum().sum() == 0


def test_build_features_renewable_has_rolling(raw):
    df = features.build_features(raw["wind"], "wind_mw")
    cols = features.feature_columns("wind")
    assert {"wind_speed_r24", "weather_max", "weather_spread"} <= set(cols)
    assert df[cols].isna().sum().sum() == 0
    assert len(df) == 24 * N_DAYS  # 재생에너지는 lag 가 없어서 행을 버리지 않는다


# --- metrics -------------------------------------------------------------------


def test_mape_skips_zero_actuals():
    assert metrics.mape(np.array([0, 100.0]), np.array([5, 110.0])) == pytest.approx(10.0)
    assert np.isnan(metrics.mape(np.zeros(3), np.ones(3)))


def test_nmae_capacity_vs_mean():
    y, p = np.array([100.0, 300.0]), np.array([150.0, 250.0])
    assert metrics.nmae(y, p, capacity=1000) == pytest.approx(5.0)
    assert metrics.nmae(y, p) == pytest.approx(25.0)
    assert np.isnan(metrics.nmae(np.zeros(2), np.ones(2)))


def test_coverage_and_evaluate():
    y = np.array([1.0, 2.0, 3.0, 4.0])
    assert metrics.coverage(y, y - 1, y + 1) == 100.0
    assert metrics.coverage(y, y + 1, y + 2) == 0.0
    res = metrics.evaluate("solar", y, y + 1, y - 2, y + 2, capacity=10.0)
    assert res["nmae"] == pytest.approx(10.0)
    assert res["primary_value"] == res["nmae"] and res["coverage"] == 100.0


def test_check_spec():
    assert metrics.check_spec("demand", 24, {"mape": 3.9, "coverage": 90})
    assert not metrics.check_spec("demand", 168, {"mape": 7.1})
    assert metrics.check_spec("solar", 24, {"nmae": 11.0})
    assert not metrics.check_spec("wind", 24, {"nmae": 10.0, "coverage": 80})
    with pytest.raises(ValueError):
        metrics.check_spec("coal", 24, {})


# --- pi ------------------------------------------------------------------------


def test_conformal_quantile_finite_sample():
    r = np.arange(1, 10, dtype=float)  # n=9 → ceil(10*0.9)/9 = 1 → 최댓값
    assert pi.conformal_quantile(r, 0.9) == 9.0
    assert np.isnan(pi.conformal_quantile(np.array([]), 0.9))


def test_single_margin_conformal():
    y = np.arange(100, dtype=float)
    f = pi.fit_conformal(y, y + 1.0)
    lower, upper = f(np.array([10.0]))
    assert f.margin == pytest.approx(1.0)
    assert (lower[0], upper[0]) == (9.0, 11.0)


def test_horizon_margins_monotonic():
    rng = np.random.default_rng(1)
    res = np.abs(rng.normal(0, 1, (50, 24))) * np.linspace(1, 5, 24)  # 먼 미래일수록 오차 큼
    m = pi.compute_horizon_margins(res, 0.9)
    assert m.shape == (24,) and np.all(np.diff(m) >= 0) and m[-1] > m[0]
    lower, upper = pi.apply_horizon_pi(np.full(24, 1.0), m)
    assert (lower >= 0).all() and np.allclose(upper, 1 + m)


def test_hourly_margins_night_vs_day():
    hours = np.tile(np.arange(24), 30)
    res = np.where((hours >= 10) & (hours <= 14), 100.0, 1.0)
    m = pi.compute_hourly_margins(res, hours, 0.9, pool=0)
    assert m[12] == 100.0 and m[0] == 1.0
    lower, upper = pi.apply_hourly_pi(np.array([0.5, 500.0]), m, np.array([0, 12]))
    assert lower.tolist() == [0.0, 400.0] and upper.tolist() == [1.5, 600.0]


# --- forecast_engine -----------------------------------------------------------


def test_recursive_forecast_demand(raw, boosters):
    hist = raw["demand"].iloc[:-48]
    out = forecast_engine.forecast_path("demand", boosters["demand"], hist, 48)
    assert len(out) == 48 and (out["predicted"] >= 0).all()
    assert out["datetime"].iloc[0] == hist["datetime"].max() + pd.Timedelta(hours=1)


def test_direct_forecast_solar_scales_by_capacity(raw, boosters):
    hist = raw["solar"].iloc[:-24]
    out = forecast_engine.forecast_path("solar", boosters["solar"], hist, 24)
    night = out[out["datetime"].dt.hour.isin([0, 1, 2])]["predicted"]
    noon = out[out["datetime"].dt.hour == 12]["predicted"].iloc[0]
    assert noon > 1000 and (night < noon / 5).all()


def test_future_exog_beyond_data_uses_profile(raw):
    exog = forecast_engine._future_exog("wind", raw["wind"], 72)  # 데이터 끝 이후 → 시간대별 평균
    assert len(exog) == 72
    assert exog[features.feature_columns("wind")].isna().sum().sum() == 0


def test_backtest_shapes(raw, boosters):
    origins = list(pd.date_range("2026-01-20", periods=3, freq="D"))
    bt = forecast_engine.backtest("wind", boosters["wind"], raw["wind"], origins, 24)
    assert bt["pred"].shape == bt["actual"].shape == bt["capacity"].shape == (3, 24)
    assert not np.isnan(bt["actual"]).any()


def test_interval_types():
    preds = np.array([10.0, 10.0])
    dts = pd.date_range("2026-01-01 11:00", periods=2, freq="h")
    hourly = {"pi_type": "hourly", "margins": list(range(24))}
    lo, hi = forecast_engine.interval(hourly, preds, dts)
    assert hi.tolist() == [21.0, 22.0]
    lo, hi = forecast_engine.interval({"pi_type": "horizon", "margins": [1.0, 2.0]}, preds, dts)
    assert hi.tolist() == [11.0, 12.0]


@pytest.fixture()
def saved_dir(tmp_path, boosters, monkeypatch):
    """학습된 모델/PI 파일이 있는 가짜 saved 폴더.
    wind 는 pi.json 을 일부러 빼서 폴백 경로를 탄다."""
    for t, b in boosters.items():
        b.save_model(str(tmp_path / f"{t}_lgbm.txt"))
    metrics_stub = {"mape": 3.0, "nmae": 2.0, "coverage": 90.0, "primary_value": 3.0}
    (tmp_path / "demand_pi.json").write_text(json.dumps({
        "pi_type": "horizon", "margins": [100.0] * 168,
        "test_metrics": {"24h": metrics_stub, "168h": metrics_stub}}))
    (tmp_path / "solar_pi.json").write_text(json.dumps({
        "pi_type": "hourly", "margins": [50.0] * 24,
        "test_metrics": {"24h": metrics_stub, "168h": metrics_stub}}))
    monkeypatch.setattr(forecast_engine, "SAVED_DIR", tmp_path)
    return tmp_path


def test_run_forecast_with_saved_pi(saved_dir):
    end = str(pd.Timestamp(START) + pd.Timedelta(days=N_DAYS - 1))
    res = forecast_engine.run_forecast("solar", START, end, 24)
    assert res["model_used"] == "lightgbm" and len(res["points"]) == 24
    assert res["metrics"]["coverage"] == 90.0
    p = res["points"][12]
    assert p["lower_bound"] <= p["predicted"] <= p["upper_bound"]


def test_run_forecast_fallback_without_pi(saved_dir):
    end = str(pd.Timestamp(START) + pd.Timedelta(days=N_DAYS - 1))
    res = forecast_engine.run_forecast("wind", START, end, 24)  # wind_pi.json 없음
    assert {"nmae", "coverage"} <= set(res["metrics"])


def test_load_model_errors(saved_dir):
    with pytest.raises(FileNotFoundError):
        forecast_engine._load_model("demand", "lstm")  # .pt 없음
    (saved_dir / "demand_lstm.pt").write_bytes(b"x")
    with pytest.raises(NotImplementedError):
        forecast_engine._load_model("demand", "lstm")
    (saved_dir / "wind_lgbm.txt").unlink()
    with pytest.raises(FileNotFoundError):
        forecast_engine._load_model("wind", "auto")


# --- router (HTTP) -------------------------------------------------------------


@pytest.fixture()
def client(saved_dir):
    path = FORECAST_DIR / "main.py"
    spec = importlib.util.spec_from_file_location("forecast_api_main_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return TestClient(module.app)


def _body(**kw):
    end = str((pd.Timestamp(START) + pd.Timedelta(days=N_DAYS - 1)).date())
    return {"start_date": START, "end_date": end, "target": "all", "horizon_hours": 24, **kw}


def test_api_forecast_all(client):
    r = client.post("/api/v1/forecast", json=_body())
    assert r.status_code == 200
    data = r.json()
    assert data["result_code"] == "00" and set(data["forecasts"]) == {"demand", "solar", "wind"}
    assert len(data["forecasts"]["demand"]["points"]) == 24


def test_api_errors(client, saved_dir):
    assert client.post("/api/v1/forecast", json=_body(horizon_hours=10)).status_code == 422
    (saved_dir / "demand_lstm.pt").write_bytes(b"x")
    r = client.post("/api/v1/forecast", json=_body(target="demand", model="lstm"))
    assert r.status_code == 501
    (saved_dir / "solar_lgbm.txt").unlink()
    r = client.post("/api/v1/forecast", json=_body(target="solar"))
    assert r.status_code == 500 and r.json()["detail"]["result_msg"] == "MODEL_LOAD_ERROR"


# --- 학습 스크립트 (train / calib / test 전체 흐름) ---------------------------------


@pytest.mark.parametrize("target", ["demand", "solar", "wind"])
def test_train_one_end_to_end(target, tmp_path, monkeypatch):
    monkeypatch.setattr(train_lgbm, "SAVED_DIR", tmp_path)
    monkeypatch.setattr(train_lgbm, "TEST_DAYS", 8)    # 합성 데이터가 35일뿐이라 구간을 줄인다
    monkeypatch.setattr(train_lgbm, "CALIB_DAYS", 9)
    result = train_lgbm.train_one(target)
    assert set(result) == {"24h", "168h"}
    saved = json.loads((tmp_path / f"{target}_pi.json").read_text(encoding="utf-8"))
    assert saved["pi_type"] == ("horizon" if target == "demand" else "hourly")
    assert len(saved["margins"]) == (168 if target == "demand" else 24)
    assert (tmp_path / f"{target}_lgbm.txt").exists()
    assert 0 <= result["24h"]["coverage"] <= 100
