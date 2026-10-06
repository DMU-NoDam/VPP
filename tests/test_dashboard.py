"""대시보드 로직(logic.py) 단위 테스트 + 화면(app.py) 스모크 테스트.

서비스끼리는 import하지 않으므로, test_health 처럼 파일 경로로 직접 로드한다.
"""

import importlib.util
import sys
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


def test_sample_dispatch_summary(df) -> None:
    s = logic.sample_dispatch_summary(df)
    assert s["saving_pct"] == pytest.approx(12.4)
    assert s["milp_cost_won"] == pytest.approx(df.fuel_cost_won.sum())


def test_parse_stochastic_sample_matches_schema_sample() -> None:
    data = logic.parse_stochastic(logic.sample_stochastic())
    assert data["vss_won"] == pytest.approx(data["eev_won"] - data["rp_won"])
    assert data["vss_pct"] == pytest.approx(3.51, abs=0.01)


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
    res = logic.call_api("GET", "http://svc/health")
    assert res.ok and res.data == {"x": 1} and res.latency_s is not None


def test_call_api_http_error(monkeypatch) -> None:
    monkeypatch.setattr(logic.requests, "request", lambda *a, **k: _FakeResponse(404))
    res = logic.call_api("POST", "http://svc/x")
    assert not res.ok and res.error == "HTTP 404"


def test_call_api_bad_json(monkeypatch) -> None:
    bad = _FakeResponse(200, bad_json=True)
    monkeypatch.setattr(logic.requests, "request", lambda *a, **k: bad)
    assert logic.call_api("GET", "http://svc/x").error == "JSON 아님"


def test_call_api_connection_error(monkeypatch) -> None:
    def boom(*a, **k):
        raise requests.ConnectionError()

    monkeypatch.setattr(logic.requests, "request", boom)
    res = logic.call_api("GET", "http://svc/x")
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
        {"ts": "a", "fuel": "lng", "mw": 5.0},
        {"ts": "b", "fuel": "lng", "mw": 7.0},
        {"ts": "b", "fuel": "solar", "mw": None},
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
