"""대시보드 로직(logic.py) 단위 테스트 + 화면(app.py) 스모크 테스트.

서비스끼리는 import하지 않으므로, test_health 처럼 파일 경로로 직접 로드한다.
"""

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import requests

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "services" / "dashboard"
CONFIG = Path(__file__).resolve().parents[1] / "config" / "dashboard.yaml"


def _load_logic():
    spec = importlib.util.spec_from_file_location("logic", DASHBOARD_DIR / "logic.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["logic"] = module  # app.py 가 `import logic` 으로 같은 모듈을 쓰게
    spec.loader.exec_module(module)
    return module


logic = _load_logic()


@pytest.fixture(scope="module")
def settings():
    return logic.load_settings(CONFIG)


@pytest.fixture(scope="module")
def df(settings):
    return logic.sample_timeseries(settings)


# ── 설정·판정 ────────────────────────────────────────────────────────────────


def test_load_settings_reads_targets_and_fleet(settings) -> None:
    assert settings.reserve_warning_pct == 7.0
    assert settings.reserve_critical_pct == 5.0
    assert set(logic.HIGHER_IS_BETTER) == set(settings.targets)
    assert [f.code for f in settings.fleet][:2] == ["nuclear", "hydro"]
    assert len(settings.crisis) >= 3


def test_load_settings_finds_repo_config() -> None:
    assert logic.load_settings().targets == logic.load_settings(CONFIG).targets


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("vss_pct", 3.0, True),
        ("vss_pct", 2.9, False),
        ("mape_24h_pct", 4.0, True),
        ("mape_24h_pct", 4.1, False),
        ("milp_latency_s", 9.9, True),
    ],
)
def test_meets(settings, key: str, value: float, expected: bool) -> None:
    assert logic.meets(settings, key, value) is expected


def test_target_text(settings) -> None:
    assert logic.target_text(settings, "milp_saving_pct") == "≥ 10%"
    assert logic.target_text(settings, "milp_latency_s") == "≤ 10초"


@pytest.mark.parametrize(
    ("reserve", "label"), [(4.9, "Critical"), (5.0, "Warning"), (6.9, "Warning"), (7.0, "Normal")]
)
def test_reserve_status(settings, reserve: float, label: str) -> None:
    assert logic.reserve_status(settings, reserve)[0] == label


# ── 목업·계산 ────────────────────────────────────────────────────────────────


def test_sample_timeseries_balances_supply(settings, df) -> None:
    assert len(df) == 24
    supply = sum(df[f"{f.code}_mw"] for f in settings.fleet) + df.shed_mw
    assert (supply - df.net_load_mw).abs().max() < 1e-6


def test_plan_ess_respects_limits(settings, df) -> None:
    ess = settings.ess
    assert df.ess_charge_mw.max() <= ess.power_mw + 1e-6
    assert df.ess_discharge_mw.max() <= ess.power_mw + 1e-6
    assert df.ess_soc_pct.min() >= -1e-6
    assert df.ess_soc_pct.max() <= 100 + 1e-6
    # 충전 에너지 x 효율 = 방전 에너지
    stored = df.ess_charge_mw.sum() * ess.round_trip_efficiency
    assert stored == pytest.approx(df.ess_discharge_mw.sum(), rel=1e-6)


def test_plan_ess_zero_capacity_does_nothing() -> None:
    charge, discharge, soc = logic.plan_ess([1.0, 2.0], logic.EssSpec(0, 0, 0.85))
    assert charge == discharge == soc == [0.0, 0.0]


def test_plan_ess_shortage_reduces_budget() -> None:
    # 첫 방전 전 충전 시간이 1시간뿐이라 에너지가 모자라면 예산을 줄여 다시 푼다.
    demand = [10.0, 100.0, 100.0]
    charge, discharge, _ = logic.plan_ess(demand, logic.EssSpec(20, 100, 0.5))
    assert sum(charge) * 0.5 == pytest.approx(sum(discharge))
    assert max(charge) <= 20


def test_peak_reduction_meets_target_with_ess(settings, df) -> None:
    before, after, pct = logic.peak_reduction(df)
    assert after < before
    assert pct == pytest.approx((before - after) / before * 100)
    assert logic.meets(settings, "ess_peak_cut_pct", pct)


def test_apply_scenario(settings, df) -> None:
    row = df.iloc[12]
    heat, drop, ramp = settings.crisis
    assert logic.apply_scenario(row, heat)[0] == pytest.approx(row.demand_mw * 1.15)
    assert logic.apply_scenario(row, drop)[1] == pytest.approx(row.available_capacity_mw - 1400)
    assert logic.apply_scenario(row, ramp)[1] == pytest.approx(
        row.available_capacity_mw - row.solar_mw * 0.5
    )


def test_crisis_table_has_row_per_scenario(settings, df) -> None:
    table = logic.crisis_table(df, settings)
    assert list(table["시나리오"]) == [sc.name for sc in settings.crisis]
    assert table["5% 유지"].dtype == bool


def test_carbon_by_fuel_matches_timeseries(settings, df) -> None:
    table = logic.carbon_by_fuel(df, settings)
    assert table["배출량(tCO2)"].sum() == pytest.approx(df.carbon_ton.sum(), abs=len(table))


def test_forecast_metrics(settings) -> None:
    fc = logic.sample_forecast(settings)
    m = logic.forecast_metrics(fc, settings)
    assert len(fc) == 168
    assert set(m) == {k for k in settings.targets if k.startswith(("mape", "pi_", "nmae"))}
    assert 0 <= m["pi_coverage_pct"] <= 100


def test_forecast_metrics_perfect_forecast(settings) -> None:
    fc = pd.DataFrame({
        "demand_actual_mw": [100.0, 200.0], "demand_fc_mw": [100.0, 200.0],
        "demand_lo_mw": [90.0, 190.0], "demand_hi_mw": [110.0, 210.0],
        "solar_actual_mw": [0.0, 5.0], "solar_fc_mw": [0.0, 5.0],
        "wind_actual_mw": [1.0, 1.0], "wind_fc_mw": [1.0, 1.0],
    })
    m = logic.forecast_metrics(fc, settings)
    assert m["mape_24h_pct"] == 0 and m["nmae_wind_pct"] == 0
    assert m["pi_coverage_pct"] == 100


def _forecast_response() -> dict:
    """forecast_api ForecastResponse 모양 (services/forecast_api/schemas.py)."""
    def target(start: str, pred: float, metrics: dict) -> dict:
        ts = pd.date_range(start, periods=3, freq="h")
        return {
            "model_used": "lightgbm", "pi_method": "conformal", "metrics": metrics,
            "points": [
                {"datetime": t.isoformat(), "predicted": pred + i,
                 "lower_bound": pred + i - 10, "upper_bound": pred + i + 10}
                for i, t in enumerate(ts)
            ],
        }

    return {
        "result_code": "00", "result_msg": "OK", "generated_at": "2026-10-23T10:00:00",
        "horizon_hours": 3,
        "forecasts": {
            "demand": target("2026-10-17 10:00", 60000.0,
                             {"mape": 0.9, "nmae": 0.88, "coverage": 90.0}),
            "solar": target("2026-09-19 00:00", 0.0, {"mape": 1800.0, "nmae": 7.2}),
            "wind": target("2026-09-19 00:00", 500.0, {"mape": 78.0, "nmae": 11.0}),
        },
    }


def test_parse_forecast() -> None:
    fc, m = logic.parse_forecast(_forecast_response())
    assert list(fc.hour) == [0, 1, 2]
    assert fc.ts.iloc[0] == pd.Timestamp("2026-10-17 10:00")
    assert (fc.demand_lo_mw < fc.demand_fc_mw).all() and (fc.demand_fc_mw < fc.demand_hi_mw).all()
    assert "demand_actual_mw" not in fc
    assert m["mape_1h_pct"] == 0.9 and m["pi_coverage_pct"] == 90.0
    assert m["nmae_solar_pct"] == 7.2 and m["nmae_wind_pct"] == 11.0
    assert m["solar_start"] == pd.Timestamp("2026-09-19")


def test_parse_forecast_demand_only() -> None:
    data = _forecast_response()
    data["forecasts"] = {"demand": data["forecasts"]["demand"]}
    _, m = logic.parse_forecast(data)
    assert "nmae_solar_pct" not in m and "solar_start" not in m


def test_parse_forecast_rejects_missing_demand() -> None:
    data = _forecast_response()
    del data["forecasts"]["demand"]
    with pytest.raises(KeyError):
        logic.parse_forecast(data)


def test_sample_dispatch_summary(settings, df) -> None:
    s = logic.sample_dispatch_summary(df, settings)
    assert s["saving_pct"] == pytest.approx(12.4)
    assert s["milp_cost_won"] == pytest.approx(df.fuel_cost_won.sum())


def test_parse_stochastic_sample_matches_schema_sample() -> None:
    data = logic.parse_stochastic(logic.sample_stochastic())
    assert data["vss_won"] == pytest.approx(data["eev_won"] - data["rp_won"])
    assert data["vss_pct"] == pytest.approx(3.51, abs=0.01)


def _milp_response(status: str = "optimal", hours: int = 24, carbon: float = 744_000.0) -> dict:
    """MilpResponse(shared/schemas.py) 에서 대시보드가 읽는 필드만 채운 응답."""
    return {
        "status": status,
        "horizon_h": hours,
        "timestamps": [f"2026-09-17T{h:02d}:00:00+09:00" for h in range(hours)],
        "model_size": {"n_variables": 2880, "n_binaries": 696, "n_constraints": 4589,
                       "build_time_s": 0.2, "solve_time_s": 1.4, "total_time_s": 1.6},
        "cost": {"fuel_won": 7.8e10, "startup_won": 3.3e8, "recourse_won": 0.0,
                 "shed_won": 0.0, "total_won": 7.86e10},
        "baseline": {"total_cost_won": 8.16e10, "cost_reduction_pct": 3.7,
                     "carbon_total_ton": 716_000.0},
        "carbon": {"total_ton": carbon, "hourly_ton": [carbon / hours] * hours,
                   "by_fuel_ton": {"bituminous_coal": carbon - 4000.0, "lng": 4000.0},
                   "intensity_ton_per_mwh": 0.5},
        "constraints": [{"name": "수급 균형", "ok": True, "detail": ""},
                        {"name": "예비율", "ok": False, "detail": "t=3"}],
        "ess": {
            "charge_mw": [100.0] * hours, "discharge_mw": [0.0] * hours,
            "soc_pct": [50.0] * hours, "peak_before_mw": 71930.0, "peak_after_mw": 68334.0,
            "peak_reduction_pct": 5.0, "target_met": True,
        },
        "notes": ["mode=cost"],
        "merit_order": [{"rank": 2, "fuel": "lng"}, {"rank": 1, "fuel": "nuclear"}],
        # 0시 충전 500 MW, 1시 방전 300 MW, 나머지 0 → Σp + ESS = 순수요
        "hours": [{"demand_mw": 50_000.0, "ess_mw": {0: -500.0, 1: 300.0}.get(h, 0.0)}
                  for h in range(hours)],
        "units": [
            {"timestamp": f"2026-09-17T{h:02d}:00:00+09:00", "unit_id": uid, "on": True,
             "p_mw": mw + {0: 500.0, 1: -300.0}.get(h, 0.0) * (uid == "lng_1")}
            for h in range(hours)
            for uid, mw in (("nuclear_1", 30_000.0), ("lng_1", 20_000.0))
        ],
    }


def test_parse_milp_reads_baseline_and_constraints() -> None:
    data = logic.parse_milp(_milp_response(), latency_s=2.9)
    assert data["day"] == date(2026, 9, 17)
    assert data["milp_cost_won"] == pytest.approx(7.86e10)
    assert data["rule_cost_won"] == pytest.approx(8.16e10)
    assert data["saving_pct"] == pytest.approx(3.7)
    assert data["latency_s"] == pytest.approx(2.9)
    assert data["solve_s"] == pytest.approx(1.4)
    assert data["constraints"] == {"수급 균형": True, "예비율": False}
    # 목업과 키가 같아야 화면 코드가 둘을 바꿔 쓸 수 있다
    assert set(logic.sample_dispatch_summary(
        pd.DataFrame({"fuel_cost_won": [1.0]}), logic.load_settings(CONFIG))) - {
        "n_scenarios"} <= set(data)


def test_parse_milp_latency_falls_back_to_server_time() -> None:
    assert logic.parse_milp(_milp_response())["latency_s"] == pytest.approx(1.6)


@pytest.mark.parametrize("status", ["infeasible", "error"])
def test_parse_milp_without_solution_is_none(status: str) -> None:
    assert logic.parse_milp(_milp_response(status)) is None


def test_parse_milp_without_baseline_is_none() -> None:
    data = _milp_response()
    data["baseline"] = None
    assert logic.parse_milp(data) is None


def test_carbon_tradeoff_compares_like_with_like() -> None:
    with_ess = logic.parse_milp(_milp_response(carbon=750_000.0))
    no_ess = logic.parse_milp(_milp_response(carbon=744_000.0))
    t = logic.carbon_tradeoff(with_ess, no_ess)
    assert t["ess_pct"] == pytest.approx((750_000 - 744_000) / 744_000 * 100)
    # Rule-based(ESS 없음) 는 ESS 없는 MILP 와 비교한다
    assert t["rule_pct"] == pytest.approx((744_000 - 716_000) / 716_000 * 100)
    assert t["rule_ton"] == pytest.approx(716_000)


def test_carbon_plan_table() -> None:
    with_ess = logic.parse_milp(_milp_response(carbon=750_000.0))
    no_ess = logic.parse_milp(_milp_response(carbon=744_000.0))
    table = logic.carbon_plan_table(with_ess, no_ess)
    assert list(table["계획"]) == ["Rule-based (ESS 없음)", "MILP (ESS 없음)", "MILP + ESS"]
    assert table["Rule-based 대비 배출"].iloc[0] == 0
    assert table["Rule-based 대비 배출"].iloc[1] == pytest.approx(3.91, abs=0.01)
    assert table["Rule-based 대비 비용"].iloc[2] == pytest.approx(-3.68, abs=0.01)
    assert pd.isna(table["배출집약도(tCO2/MWh)"].iloc[0])


def test_milp_carbon_by_fuel() -> None:
    with_ess = logic.parse_milp(_milp_response(carbon=750_000.0))
    no_ess = logic.parse_milp(_milp_response(carbon=744_000.0))
    table = logic.milp_carbon_by_fuel(with_ess, no_ess, {"lng": "가스"})
    assert list(table["발전원"]) == ["bituminous_coal", "가스"]
    assert table["차이(tCO2)"].sum() == 6000


def test_load_unit_fuels_reads_repo_config() -> None:
    fuels = logic.load_unit_fuels(CONFIG.parent / "generators.yaml")
    assert fuels["nuclear_1"] == "nuclear"
    assert set(fuels.values()) <= {"nuclear", "bituminous_coal", "anthracite", "lng", "oil",
                                   "hydro"}


def test_milp_fuel_stack_balances_net_demand() -> None:
    milp = logic.parse_milp(_milp_response())
    stack = logic.milp_fuel_stack(milp, {"nuclear_1": "nuclear", "lng_1": "lng"})
    # merit order 순 (rank 1 원자력 먼저), ESS 는 뒤에
    assert list(dict.fromkeys(stack.fuel)) == ["nuclear", "lng", "ess_discharge", "ess_charge"]
    # 시간마다 발전 + (방전 − 충전) = 순수요
    by_hour = stack.groupby("hour")["mw"].sum()
    assert by_hour.tolist() == pytest.approx(milp["demand_mw"])
    assert stack.loc[stack.fuel == "ess_charge", "mw"].tolist() == [-500.0]


def test_milp_fuel_stack_unknown_unit_and_no_units() -> None:
    milp = logic.parse_milp(_milp_response())
    assert "unknown" in set(logic.milp_fuel_stack(milp, {"nuclear_1": "nuclear"}).fuel)
    milp["units"] = []
    assert logic.milp_fuel_stack(milp, {}).empty


def test_milp_ess_frame() -> None:
    frame = logic.milp_ess_frame(_milp_response(hours=24)["ess"])
    assert list(frame.columns) == ["hour", "ess_charge_mw", "ess_discharge_mw", "ess_soc_pct"]
    assert len(frame) == 24 and frame.hour.iloc[-1] == 23


# ── REST ─────────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int, body=None, bad_json: bool = False) -> None:
        self.status_code = status
        self._body = body
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._body


def test_call_api_ok(monkeypatch) -> None:
    monkeypatch.setattr(logic.requests, "request", lambda *a, **k: _FakeResponse(200, {"x": 1}))
    res = logic.call_api("GET", "http://svc/health", timeout=1.0)
    assert res.ok and res.data == {"x": 1} and res.latency_s is not None


def test_call_api_http_error(monkeypatch) -> None:
    monkeypatch.setattr(logic.requests, "request", lambda *a, **k: _FakeResponse(404))
    res = logic.call_api("POST", "http://svc/x", timeout=1.0)
    assert not res.ok and res.error == "HTTP 404"


def test_call_api_bad_json(monkeypatch) -> None:
    bad = _FakeResponse(200, bad_json=True)
    monkeypatch.setattr(logic.requests, "request", lambda *a, **k: bad)
    assert logic.call_api("GET", "http://svc/x", timeout=1.0).error == "JSON 아님"


def test_call_api_connection_error(monkeypatch) -> None:
    def boom(*a, **k):
        raise requests.ConnectionError()

    monkeypatch.setattr(logic.requests, "request", boom)
    res = logic.call_api("GET", "http://svc/x", timeout=1.0)
    assert not res.ok and res.error == "ConnectionError"


def test_service_urls_from_env(monkeypatch) -> None:
    monkeypatch.setenv("DISPATCH_URL", "http://dispatch_api:8002")
    assert logic.service_urls()["dispatch_api"] == "http://dispatch_api:8002"


def test_latest_row_and_mix() -> None:
    rows = [{"ts": "a", "demand_mw": 1}, {"ts": "b", "demand_mw": 2}]
    demand = logic.ApiResult(data={"rows": rows})
    assert logic.latest_row(demand)["demand_mw"] == 2
    assert logic.latest_row(logic.ApiResult(error="down")) is None

    mix = logic.ApiResult(data={"rows": [
        {"ts": "a", "fuel": "lng", "capacity_mw": 9.0, "trade_mwh": 5.0},
        {"ts": "b", "fuel": "lng", "capacity_mw": 9.0, "trade_mwh": 7.0},
        {"ts": "b", "fuel": "solar", "capacity_mw": 9.0, "trade_mwh": None},
        {"ts": "b", "fuel": "wind", "capacity_mw": 9.0, "trade_mwh": "None"},
    ]})
    assert logic.latest_mix(mix) == {"lng": 7.0}
    assert logic.latest_mix(logic.ApiResult(data={"rows": []})) == {}


# ── 조정·표시 ────────────────────────────────────────────────────────────────


def test_adjust_default_equals_base(settings, df) -> None:
    pd.testing.assert_frame_equal(logic.sample_timeseries(settings, adjust=logic.Adjust()), df)


def test_adjust_demand_and_outage(settings, df) -> None:
    adj = logic.sample_timeseries(settings, adjust=logic.Adjust(demand_pct=110, outage_mw=1400))
    assert adj.demand_mw.sum() == pytest.approx(df.demand_mw.sum() * 1.1)
    assert (df.available_capacity_mw - adj.available_capacity_mw).round(6).eq(1400).all()
    assert adj.coal_mw.sum() + adj.lng_mw.sum() > df.coal_mw.sum() + df.lng_mw.sum()


def test_adjust_ess_spec_override(settings) -> None:
    spec = logic.Adjust(ess_power_mw=0).ess_spec(settings.ess)
    assert spec.power_mw == 0 and spec.energy_mwh == settings.ess.energy_mwh
    no_ess = logic.sample_timeseries(settings, adjust=logic.Adjust(ess_power_mw=0))
    assert logic.peak_reduction(no_ess)[2] == 0


@pytest.mark.parametrize(
    ("func", "value", "compact", "expected"),
    [
        ("fmt_power", 84780, True, "84.8 GW"),
        ("fmt_power", 84780, False, "84,780 MW"),
        ("fmt_power", 950, True, "950 MW"),
        ("fmt_energy", 8100, True, "8.1 GWh"),
        ("fmt_won", 1.593e10, True, "159.3억원"),
        ("fmt_won", 2.5e12, True, "2.5조원"),
        ("fmt_won", -3.2e5, True, "-32.0만원"),
        ("fmt_won", 999, True, "999원"),
        ("fmt_won", 1.5e8, False, "150,000,000원"),
        ("fmt_ton", 747029, True, "747.0천 tCO2"),
        ("fmt_ton", 500, True, "500 tCO2"),
    ],
)
def test_formatters(func: str, value: float, compact: bool, expected: str) -> None:
    assert getattr(logic, func)(value, compact) == expected


def test_compare_table_flags_shortage(settings, df) -> None:
    adj = logic.sample_timeseries(settings, adjust=logic.Adjust(demand_pct=115, outage_mw=1400))
    table = logic.compare_table(settings, df, adj).set_index("지표")
    assert table.loc["하루 최저 예비율", "판정"] == "✗ 미달"
    assert table.loc["공급 부족량", "기본"] == "0 MWh"
    assert table.loc["공급 부족량", "변화"].startswith("+")
    assert table.loc["원 수요 피크", "변화"] == "+15.0%"


def test_compare_table_same_input_has_no_change(settings, df) -> None:
    table = logic.compare_table(settings, df, df, compact=False)
    assert set(table["변화"]) <= {"+0.0%", "+0.0%p", "—"}


def test_hourly_table(settings, df) -> None:
    table = logic.hourly_table(settings, df)
    assert len(table) == 24 and table["시각"].iloc[0] == "00:00"
    assert "원자력(MW)" in table.columns and "예비율(%)" in table.columns


# ── 분석 기간 (탄소·ESS) ─────────────────────────────────────────────────────


def test_sample_day_is_deterministic_and_weekday_aware(settings) -> None:
    from datetime import date

    thu, sun = date(2026, 10, 1), date(2026, 10, 4)
    a, b = logic.sample_day(settings, thu), logic.sample_day(settings, thu)
    pd.testing.assert_frame_equal(a, b)
    assert len(a) == 24 and (a["date"] == thu).all()
    assert a.ts.iloc[19] == pd.Timestamp("2026-10-01 19:00")
    # 일요일 계수(0.85)가 목요일(1.01)보다 작다
    assert logic.sample_day(settings, sun).demand_mw.sum() < a.demand_mw.sum()


def test_clamp_period_orders_and_limits() -> None:
    from datetime import date, timedelta

    d = date(2026, 10, 1)
    assert logic.clamp_period(d, d - timedelta(days=2)) == (d - timedelta(days=2), d)
    start, end = logic.clamp_period(d - timedelta(days=100), d)
    assert end == d and (end - start).days + 1 == logic.MAX_PERIOD_DAYS


def test_sample_period_and_daily_summary(settings) -> None:
    from datetime import date

    df = logic.sample_period(settings, date(2026, 9, 25), date(2026, 10, 1))
    assert len(df) == 7 * 24
    assert df.ts.is_monotonic_increasing

    daily = logic.daily_summary(df)
    assert len(daily) == 7
    assert daily["배출(tCO2)"].sum() == pytest.approx(df.carbon_ton.sum(), rel=1e-3)
    # ESS 는 하루 단위 피크 셰이빙이라 날마다 피크가 줄어든다
    assert (daily["피크 감소율(%)"] > 0).all()


# ── 화면 스모크 ──────────────────────────────────────────────────────────────


def test_app_renders_without_services(monkeypatch) -> None:
    """모든 서비스가 꺼져 있어도 목업으로 한 화면이 끝까지 그려진다."""
    from streamlit.testing.v1 import AppTest

    def down(*a, **k):
        raise requests.ConnectionError()

    monkeypatch.setattr(logic.requests, "request", down)
    monkeypatch.syspath_prepend(str(DASHBOARD_DIR))

    at = AppTest.from_file(str(DASHBOARD_DIR / "app.py"), default_timeout=60).run()
    assert not at.exception
    assert at.selectbox(key="crisis_scenario").options[0] == "없음"

    at.selectbox(key="crisis_scenario").select(at.selectbox(key="crisis_scenario").options[1]).run()
    at.sidebar.slider[0].set_value(3).run()
    assert not at.exception


@pytest.fixture
def fresh_cache():
    """st.cache_data 는 프로세스 단위라 AppTest 끼리 공유된다. 앞뒤 테스트에 결과가 새지 않게
    시작과 끝에 비운다."""
    import streamlit as st
    st.cache_data.clear()
    yield
    st.cache_data.clear()


def test_app_renders_with_milp(monkeypatch, fresh_cache) -> None:
    """dispatch_api 만 살아 있으면 최적화 결과·ESS·탄소 패널이 MILP 응답으로 그려진다."""
    from streamlit.testing.v1 import AppTest

    calls: list[dict] = []

    def fake(method, url, json=None, params=None, timeout=None):
        if url.endswith(":8002/health"):
            return _FakeResponse(200, {"status": "ok"})
        if url.endswith("/dispatch/milp"):
            calls.append(json)
            return _FakeResponse(200, _milp_response())
        raise requests.ConnectionError()

    monkeypatch.setattr(logic.requests, "request", fake)
    monkeypatch.delenv("DISPATCH_URL", raising=False)
    monkeypatch.syspath_prepend(str(DASHBOARD_DIR))

    at = AppTest.from_file(str(DASHBOARD_DIR / "app.py"), default_timeout=60).run()
    assert not at.exception
    page = "".join(m.value for m in at.markdown)
    assert "MILP 09/17 계획" in page          # 최적화 결과 (cost 모드)
    assert "MILP·peak 09/17 계획" in page     # ESS (peak_shaving 모드)
    assert "Rule-based 대비" in page          # 탄소 (ESS 연계 / ESS 없음)
    assert "순수요 기준" in page              # Merit Order 계획 스택
    # 기본 계획만 유닛 출력을 받는다
    assert {(c.get("ess_mode"), c.get("use_ess", True), c["include_units"]) for c in calls} == {
        (None, True, True), ("peak_shaving", True, False), (None, False, False)}


def test_app_adjust_controls(monkeypatch) -> None:
    """조정 슬라이더 → 비교표 표시, 단위 토글, 되돌리기가 동작한다."""
    from streamlit.testing.v1 import AppTest

    def down(*a, **k):
        raise requests.ConnectionError()

    monkeypatch.setattr(logic.requests, "request", down)
    monkeypatch.syspath_prepend(str(DASHBOARD_DIR))

    at = AppTest.from_file(str(DASHBOARD_DIR / "app.py"), default_timeout=60).run()
    tables_before = len(at.dataframe)

    at.slider(key="adj_demand").set_value(115).run()
    at.slider(key="adj_outage").set_value(1400).run()
    assert not at.exception
    assert len(at.dataframe) == tables_before + 1  # 비교표가 나타난다

    at.toggle(key="compact_units").set_value(False).run()
    assert not at.exception

    reset = next(b for b in at.button if "되돌리기" in b.label)
    reset.click().run()
    assert at.slider(key="adj_demand").value == 100
    assert len(at.dataframe) == tables_before


def test_app_period_input(monkeypatch) -> None:
    """분석 기간을 7일로 바꿔도 한 화면이 오류 없이 그려진다."""
    from datetime import date, timedelta

    from streamlit.testing.v1 import AppTest

    def down(*a, **k):
        raise requests.ConnectionError()

    monkeypatch.setattr(logic.requests, "request", down)
    monkeypatch.syspath_prepend(str(DASHBOARD_DIR))

    at = AppTest.from_file(str(DASHBOARD_DIR / "app.py"), default_timeout=60).run()
    today = date.today()
    at.date_input(key="period").set_value((today - timedelta(days=6), today)).run()
    assert not at.exception
    assert any("7일" in m.value for m in at.markdown)


@pytest.mark.parametrize("page", ["page_analysis", "page_data"])
def test_detail_pages_render(monkeypatch, page) -> None:
    """분석 상세·시간대 데이터 페이지가 7일 기간으로도 끝까지 그려진다."""
    from streamlit.testing.v1 import AppTest

    def down(*a, **k):
        raise requests.ConnectionError()

    monkeypatch.setattr(logic.requests, "request", down)
    monkeypatch.syspath_prepend(str(DASHBOARD_DIR))

    script = f"""
from datetime import date, timedelta
import streamlit as st
import app
st.session_state.setdefault("period", (date.today() - timedelta(days=6), date.today()))
st.session_state["vpp_ctx"] = app.build_context()
app.{page}()
"""
    at = AppTest.from_string(script, default_timeout=60).run()
    assert not at.exception
    assert len(at.dataframe) >= 1


# ── 시나리오 생성기 페이지 ───────────────────────────────────────────────────


def _scenario_page_script() -> None:
    """AppTest 가 실행할 스크립트: 시나리오 페이지만 단독으로 띄운다."""
    import page_scenarios
    import ui

    ui.inject_custom_css()
    page_scenarios.render()


def _fake_dispatch(monkeypatch):
    """requests 를 가로채 dispatch_api 대신 생성기를 직접 불러 응답한다."""
    sys.path.insert(0, str(DASHBOARD_DIR.parent / "dispatch_api"))
    import scenarios
    from shared.schemas import ScenarioRequest

    calls = []

    class Resp:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake(method, url, json=None, **kwargs):
        calls.append(json)
        body = scenarios.generate(ScenarioRequest(**json)).model_dump(mode="json")
        return Resp(body)

    monkeypatch.setattr(logic.requests, "request", fake)
    return calls


def test_scenario_page_renders_and_regenerates(monkeypatch) -> None:
    from streamlit.testing.v1 import AppTest

    calls = _fake_dispatch(monkeypatch)
    monkeypatch.syspath_prepend(str(DASHBOARD_DIR))

    at = AppTest.from_function(_scenario_page_script, default_timeout=60).run()
    assert not at.exception
    assert calls[-1]["n_scenarios"] == 10
    assert any("생성 결과" in m.value for m in at.markdown)

    at.radio(key="scn_var").set_value("태양광").run()
    assert not at.exception

    at.sidebar.slider[0].set_value(12)            # 시나리오 수 (폼 안이라 제출 전엔 반영 안 됨)
    next(b for b in at.button if b.label == "시나리오 생성").click().run()
    assert not at.exception
    assert calls[-1]["n_scenarios"] == 12

    next(b for b in at.button if b.label == "기본 조건으로").click().run()
    assert at.session_state.scn_params["n_scenarios"] == 10


def test_scenario_visible_picker(monkeypatch) -> None:
    """시나리오 경로 그래프에 그릴 시나리오를 고를 수 있다 (기본 전체)."""
    from streamlit.testing.v1 import AppTest

    _fake_dispatch(monkeypatch)
    monkeypatch.syspath_prepend(str(DASHBOARD_DIR))

    def shown() -> str:
        return next(c.value for c in at.caption if "개 표시" in c.value)

    at = AppTest.from_function(_scenario_page_script, default_timeout=60).run()
    assert "10/10개 표시" in shown()

    next(b for b in at.button if b.label == "모두 숨기기").click().run()
    assert not at.exception
    assert "0/10개 표시" in shown()
    assert at.session_state.scn_visible_10 == []

    at.session_state.scn_visible_10 = ["S02", "S05"]
    at.run()
    assert "2/10개 표시" in shown()
    assert "S01 는 숨김" in shown()  # 표에서 선택된 기본값 S01 이 숨겨졌음을 알린다

    next(b for b in at.button if b.label == "전체 표시").click().run()
    assert "10/10개 표시" in shown()


def test_scenario_page_shows_error_when_dispatch_down(monkeypatch) -> None:
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    st.cache_data.clear()  # 앞 테스트가 캐시한 성공 응답을 지운다

    def down(*a, **k):
        raise requests.ConnectionError()

    monkeypatch.setattr(logic.requests, "request", down)
    monkeypatch.syspath_prepend(str(DASHBOARD_DIR))

    at = AppTest.from_function(_scenario_page_script, default_timeout=60).run()
    assert not at.exception
    assert at.error and "dispatch_api" in at.error[0].value

    # 서비스가 살아나면 캐시된 실패 없이 바로 결과가 나온다
    calls = _fake_dispatch(monkeypatch)
    at.run()
    assert not at.error and calls


def test_scenario_page_helpers() -> None:
    sys.path.insert(0, str(DASHBOARD_DIR))
    import page_scenarios

    data = {
        "scenarios": [{"scenario_id": "S01", "label": "고수요", "probability": 1.0,
                       "peak_net_load_mw": 5.0, "demand_dev_pct": 2.0, "solar_dev_pct": 0.0,
                       "wind_dev_pct": 0.0, "demand_mw": [10.0, 9.0], "solar_mw": [1.0, 0.0],
                       "wind_mw": [2.0, 2.0]}],
    }
    assert page_scenarios.series(data["scenarios"][0], "net") == [7.0, 7.0]
    long = page_scenarios.long_table(data)
    assert list(long["net_load_mw"]) == [7.0, 7.0] and len(long) == 2
    assert page_scenarios.scenario_frame(data).loc[0, "라벨"] == "고수요"


# ── Two-Stage 성능 비교 (테스트) 페이지 ──────────────────────────────────────


def _stochastic_page_script() -> None:
    """AppTest 가 실행할 스크립트: Two-Stage 테스트 페이지만 단독으로 띄운다."""
    import page_stochastic_test
    import ui

    ui.inject_custom_css()
    page_stochastic_test.render()


def _stochastic_body(req: dict, status: str = "optimal", rp: float = 9.5e10) -> dict:
    """StochasticResponse 모양의 가짜 응답 (솔버를 돌리지 않는다)."""
    eev = 1.0e11
    cost = {"fuel_won": rp * 0.9, "startup_won": rp * 0.04, "recourse_won": rp * 0.06,
            "shed_won": 0.0, "total_won": rp}
    ws = rp * 0.99 if req.get("compute_ws") else None
    return {
        "status": status, "solve_time_s": 12.3, "start": req["start"],
        "horizon_h": 2, "n_scenarios": len(req["scenarios"]),
        "first_stage": [
            {"timestamp": f"2026-09-22T0{t}:00:00", "unit_id": uid, "on": on, "p_mw": 100.0}
            for uid, on in (("nuclear_1", True), ("lng_1", False)) for t in range(2)
        ],
        "expected_cost": cost, "expected_hours": [],
        "scenarios": [
            {"scenario_id": sc["scenario_id"], "probability": sc["probability"], "cost": cost,
             "shed_mwh": 0.0, "rt_adjust_mwh": 120.0, "hours": None}
            for sc in req["scenarios"]
        ],
        "vss": {"rp_won": rp, "ev_won": 9.0e10, "eev_won": eev, "vss_won": eev - rp,
                "vss_pct": (eev - rp) / eev * 100, "ws_won": ws,
                "evpi_won": None if ws is None else rp - ws, "target_met": True},
    }


def _fake_dispatch_with_stochastic(monkeypatch):
    """/scenarios 는 생성기를 직접 부르고, /dispatch/stochastic 은 가짜 응답을 준다."""
    sys.path.insert(0, str(DASHBOARD_DIR.parent / "dispatch_api"))
    import scenarios
    from shared.schemas import ScenarioRequest

    calls = []

    class Resp:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake(method, url, json=None, **kwargs):
        calls.append((url, json, kwargs.get("timeout")))
        if url.endswith("/dispatch/stochastic"):
            return Resp(_stochastic_body(json))
        return Resp(scenarios.generate(ScenarioRequest(**json)).model_dump(mode="json"))

    monkeypatch.setattr(logic.requests, "request", fake)
    return calls


def test_stochastic_page_waits_then_runs(monkeypatch) -> None:
    from streamlit.testing.v1 import AppTest

    calls = _fake_dispatch_with_stochastic(monkeypatch)
    monkeypatch.syspath_prepend(str(DASHBOARD_DIR))

    at = AppTest.from_function(_stochastic_page_script, default_timeout=60).run()
    assert not at.exception
    assert at.info and not calls  # '실행'을 누르기 전에는 호출하지 않는다

    next(b for b in at.button if b.label == "실행").click().run()
    assert not at.exception
    url, body, timeout = calls[-1]
    assert url.endswith("/dispatch/stochastic")
    assert len(body["scenarios"]) == 10 and body["solver"]["time_limit_s"] == 60
    assert timeout == 60 * 3 + 60
    assert any("VSS 목표 달성" in m.value for m in at.markdown)
    assert len(at.session_state.sto_history) == 1

    next(b for b in at.button if b.label == "이력 지우기").click().run()
    assert not at.exception


def test_stochastic_page_shows_error_when_dispatch_down(monkeypatch) -> None:
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    st.cache_data.clear()

    def down(*a, **k):
        raise requests.ConnectionError()

    monkeypatch.setattr(logic.requests, "request", down)
    monkeypatch.syspath_prepend(str(DASHBOARD_DIR))

    at = AppTest.from_function(_stochastic_page_script, default_timeout=60)
    at.session_state.sto_params = {"seed": 1, "n_scenarios": 10, "time_limit_s": 10,
                                   "mip_gap_pct": 0.5, "compute_ws": False}
    at.run()
    assert not at.exception
    assert at.error and "dispatch_api" in at.error[0].value


def test_stochastic_page_helpers() -> None:
    sys.path.insert(0, str(DASHBOARD_DIR))
    import page_stochastic_test as page

    req = {"start": "2026-09-22T00:00:00", "compute_ws": True,
           "scenarios": [{"scenario_id": "S01", "probability": 1.0}]}
    good = page.summarize(_stochastic_body(req), mip_gap=0.005)
    assert good["vss_pct"] == pytest.approx(5.0) and good["order_ok"] and good["significant"]
    assert good["evpi_won"] == pytest.approx(9.5e10 * 0.01)
    assert page.verdict(good, 3.0)[1] == "ok"
    assert page.verdict(good, 6.0)[1] == "critical"

    # RP 가 덜 풀려 EEV 보다 비싸면 VSS 가 음수 — 판정을 보류한다
    stuck = page.summarize(_stochastic_body(req, status="time_limit", rp=1.09e11), 0.005)
    assert stuck["vss_pct"] < 0 and not stuck["order_ok"]
    assert page.verdict(stuck, 3.0) == ("판정 보류 — 미수렴 (time_limit)", "warn")
    broken = page.summarize(_stochastic_body(req, rp=1.09e11), 0.005)
    assert page.verdict(broken, 3.0)[1] == "critical"
    # 수렴했어도 gap x EEV 보다 작은 VSS 는 솔버 오차와 구분되지 않는다
    tiny = page.summarize(_stochastic_body(req, rp=0.998e11), 0.005)
    assert tiny["order_ok"] and not tiny["significant"]
    assert page.verdict(tiny, 3.0)[1] == "warn"

    body = _stochastic_body(req)
    assert list(page.problem_frame(good)["문제"]) == ["WS", "RP", "EEV", "EV"]
    assert page.breakdown_frame(body)["비중(%)"].sum() == pytest.approx(100.0)
    assert page.scenario_frame(body, {"S01": "고수요"}).loc[0, "라벨"] == "고수요"
    matrix = page.commitment_matrix(body)
    assert list(matrix.index) == ["nuclear_1", "lng_1"]
    assert matrix.loc["nuclear_1", 1] == 100.0 and matrix.loc["lng_1", 0] == 0.0
    assert page.request_timeout(10, 10, True) == 10 * 13 + 60


# ── collector 실측 ───────────────────────────────────────────────────────────


def _trading_rows(days: list[str], skip: tuple[str, str] | None = None) -> list[dict]:
    """trading_by_fuel 응답 행. skip=(날짜, 발전원) 이면 그날 그 발전원은 결측."""
    rows = []
    for day in days:
        for h in range(24):
            ts = f"{day} {h:02d}:00:00"
            for fuel, mw in (("nuclear", 20000.0), ("bituminous_coal", 25000.0),
                             ("lng", 12000.0), ("solar", 1000.0), ("pumped", 300.0)):
                val = "None" if skip == (day, fuel) else mw
                rows.append({"ts": ts, "fuel": fuel, "capacity_mw": 1.0, "trade_mwh": val})
    return rows


def test_latest_demand_takes_last_and_hour_ago() -> None:
    rows = [{"ts": f"2026-10-17 {8 + (m // 60):02d}:{m % 60:02d}:00", "demand_mw": 50000.0 + m}
            for m in range(0, 65, 5)]
    res = logic.ApiResult(data={"rows": rows})
    d = logic.latest_demand(res)
    assert d["ts"] == pd.Timestamp("2026-10-17 09:00") and d["mw"] == 50060.0
    assert d["prev_mw"] == 50000.0


def test_latest_demand_missing() -> None:
    assert logic.latest_demand(logic.ApiResult(error="down")) is None
    res = logic.ApiResult(data={"rows": [{"ts": "2026-10-17 09:00:00", "demand_mw": "None"}]})
    assert logic.latest_demand(res) is None


def test_latest_full_day_skips_incomplete_day() -> None:
    res = logic.ApiResult(data={"rows": _trading_rows(
        ["2026-09-17", "2026-09-18"], skip=("2026-09-18", "nuclear"))})
    trading = logic.rows_frame(res, ("trade_mwh",))
    assert logic.latest_full_day(trading) == date(2026, 9, 17)


def test_latest_full_day_none_when_fuel_absent() -> None:
    rows = [r for r in _trading_rows(["2026-09-17"]) if r["fuel"] != "lng"]
    trading = logic.rows_frame(logic.ApiResult(data={"rows": rows}), ("trade_mwh",))
    assert logic.latest_full_day(trading) is None


def test_merit_fuel_order_by_latest_cost() -> None:
    cost = pd.DataFrame({
        "month": ["2026-07", "2026-08", "2026-08", "2026-08"],
        "fuel": ["lng", "lng", "bituminous_coal", "nuclear"],
        "cost_won_per_kwh": [1.0, 155.0, 90.0, 6.5],
    })
    fuels = ["pumped", "lng", "solar", "nuclear", "bituminous_coal", "unknown"]
    assert logic.merit_fuel_order(fuels, cost) == [
        "solar", "nuclear", "bituminous_coal", "lng", "unknown", "pumped",
    ]


def test_real_mix_same_day_demand() -> None:
    trading = logic.rows_frame(logic.ApiResult(data={"rows": _trading_rows(
        ["2026-09-16", "2026-09-17"])}), ("trade_mwh",))
    demand = pd.DataFrame({
        "ts": ["2026-09-17 00:00:00", "2026-09-17 00:05:00", "2026-09-17 01:00:00"],
        "demand_mw": [60000.0, 62000.0, 59000.0],
    })
    mix = logic.real_mix(trading, demand, date(2026, 9, 17))
    assert set(mix.hour) == set(range(24))
    assert mix.groupby("hour").mw.sum().iloc[0] == pytest.approx(58300.0)
    assert mix.loc[mix.hour == 0, "demand_mw"].iloc[0] == pytest.approx(61000.0)
    assert mix.loc[mix.hour == 5, "demand_mw"].isna().all()


def _mix() -> pd.DataFrame:
    """real_mix 모양의 이틀 시간 분량 (hour 0·1)."""
    return pd.DataFrame({
        "hour": [0, 0, 0, 1, 1, 1],
        "fuel": ["bituminous_coal", "lng", "solar", "bituminous_coal", "lng", "solar"],
        "mw": [1000.0, 500.0, 0.0, 800.0, 400.0, 200.0],
        "capacity_mw": [2000.0, 1000.0, 400.0, 2000.0, 1000.0, 400.0],
    })


def test_load_fuel_co2_reads_generators_yaml() -> None:
    co2 = logic.load_fuel_co2()
    assert co2["bituminous_coal"] > co2["lng"] > co2["nuclear"]
    assert "solar" not in co2


def test_load_fuel_co2_missing_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(logic, "_config_candidates", lambda *a, **k: [tmp_path / "x.yaml"])
    assert logic.load_fuel_co2() == {}


def test_real_carbon_by_hour_zero_for_unknown_fuel() -> None:
    carbon = logic.real_carbon_by_hour(_mix(), {"bituminous_coal": 0.9, "lng": 0.5})
    assert carbon[0] == pytest.approx(1000 * 0.9 + 500 * 0.5)
    assert carbon[1] == pytest.approx(800 * 0.9 + 400 * 0.5)


def test_real_carbon_by_fuel_follows_order() -> None:
    table = logic.real_carbon_by_fuel(
        _mix(), {"bituminous_coal": 0.9}, {"bituminous_coal": "유연탄"},
        ["solar", "bituminous_coal", "lng", "oil"],
    )
    assert list(table["발전원"]) == ["solar", "유연탄", "lng"]
    assert table.loc[1, "발전량(MWh)"] == 1800 and table.loc[1, "배출량(tCO2)"] == 1620
    assert table.loc[2, "배출량(tCO2)"] == 0


def test_real_utilization_top_and_rest() -> None:
    u = logic.real_utilization(_mix(), 1, top=2)
    assert list(u.fuel) == ["bituminous_coal", "lng", "기타"]
    assert u.loc[0, "util_pct"] == pytest.approx(40.0)
    assert u.loc[2, "mw"] == 200.0 and u.loc[2, "util_pct"] == pytest.approx(50.0)


def test_parse_stochastic_latest() -> None:
    latest = {
        "state": "running", "computed_at": "2026-10-23T15:31:00+09:00", "seed": 1,
        "n_scenarios": 10, "reserve_ratio": 0.1, "shed_penalty_won_per_mwh": 1e7,
        "time_limit_s": 60.0, "mip_gap": 0.001, "result": logic.sample_stochastic(),
        "error": None,
    }
    data = logic.parse_stochastic_latest(latest)
    assert data["vss_pct"] == pytest.approx(3.51, abs=0.01)
    assert data["seed"] == 1 and data["reserve_ratio"] == 0.1 and data["state"] == "running"
    assert data["computed_at"].hour == 15
    assert logic.parse_stochastic_latest({**latest, "result": None}) is None


# ── config/dashboard.yaml ({value, unit, source}) ───────────────────────────


def test_cfg_value_checks_unit() -> None:
    node = {"value": 5.0, "unit": "%", "source": "x"}
    assert logic.cfg_value(node, "%") == 5.0
    with pytest.raises(ValueError):
        logic.cfg_value(node, "ratio")
    with pytest.raises(ValueError):
        logic.cfg_value(5.0, "%")  # 맨 숫자는 받지 않는다


def test_load_settings_rejects_wrong_unit(tmp_path) -> None:
    import yaml

    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    raw["ess"]["power_mw"]["unit"] = "kW"
    bad = tmp_path / "dashboard.yaml"
    bad.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError):
        logic.load_settings(bad)


def test_page_defaults_keep_types(settings) -> None:
    scn = settings.page_defaults["scenarios"]
    assert isinstance(scn["seed"], int) and isinstance(scn["n_samples"], int)
    assert isinstance(scn["demand_sigma_pct"], float)
    sto = settings.page_defaults["stochastic_test"]
    assert isinstance(sto["time_limit_s"], int) and sto["compute_ws"] is False


def test_crisis_ok_column_follows_target(settings) -> None:
    col = logic.crisis_ok_column(settings)
    assert col == f"{settings.targets['crisis_reserve_pct']:g}% 유지"
