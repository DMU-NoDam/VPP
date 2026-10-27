"""Two-Stage Stochastic(dispatch_api/stochastic.py)·발전기 로더(units.py) 단위 테스트.

솔버 시간을 줄이려고 작은 계통(4유닛 x 6시간 x 3시나리오)으로 성질만 확인한다.
"""

import dataclasses
import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from shared.schemas import Scenario, SolverOptions

DISPATCH_DIR = Path(__file__).resolve().parents[1] / "services" / "dispatch_api"


def _load(name: str):
    if str(DISPATCH_DIR) not in sys.path:
        sys.path.insert(0, str(DISPATCH_DIR))
    spec = importlib.util.spec_from_file_location(name, DISPATCH_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


units = _load("units")
stochastic = _load("stochastic")

START = datetime(2026, 9, 22)
TIGHT = SolverOptions(time_limit_s=30, mip_gap=1e-6)
# 작은 계통 테스트용 고정값 (config 와 무관하게 성질만 본다)
RUN_KW = dict(reserve_ratio=0.10, shed_penalty=10_000_000.0)
PARAMS = stochastic.StochasticParams(
    reserve_short_penalty_krw_per_mw=1_000_000.0, vss_target_pct=3.0
)


def unit(uid: str, fuel: str, p_max: float, p_min: float, cost: float, **kw) -> "units.Unit":
    base = dict(
        ramp_up_mw_per_h=p_max, ramp_down_mw_per_h=p_max, min_up_h=1, min_down_h=1,
        startup_cost_krw=1_000_000, co2_ton_per_mwh=0.5, u_init=0, init_state_h=1,
    )
    base.update(kw)
    return units.Unit(id=uid, fuel=fuel, p_max_mw=p_max, p_min_mw=p_min,
                      fuel_cost_krw_per_mwh=cost, **base)


# 느린 기저 1기(켜져 있음, 최소가동 6h) + 느린 중간 1기(꺼진 지 1h 라 5h 더 못 켬) + 속응 LNG 2기
FLEET = [
    unit("base", "nuclear", 600, 300, 10_000, min_up_h=6, min_down_h=6, u_init=1, init_state_h=6,
         ramp_up_mw_per_h=100, ramp_down_mw_per_h=100),
    unit("mid", "bituminous_coal", 400, 150, 60_000, min_up_h=6, min_down_h=6,
         startup_cost_krw=30_000_000),
    unit("lng_a", "lng", 300, 100, 150_000, min_up_h=2, min_down_h=2, init_state_h=2),
    unit("lng_b", "lng", 300, 100, 150_000, min_up_h=2, min_down_h=2, init_state_h=2),
]


def scen(sid: str, prob: float, demand: list[float]) -> Scenario:
    return Scenario(scenario_id=sid, probability=prob, demand_mw=demand,
                    solar_mw=[0.0] * len(demand), wind_mw=[50.0] * len(demand))


SCENARIOS = [
    scen("low", 0.3, [700, 720, 750, 780, 760, 730]),
    scen("mid", 0.4, [800, 850, 900, 950, 900, 850]),
    scen("high", 0.3, [900, 980, 1100, 1250, 1150, 1000]),
]


@pytest.fixture(scope="module")
def params():
    return PARAMS


@pytest.fixture(scope="module")
def result(params):
    return stochastic.run(FLEET, SCENARIOS, params, START, opts=TIGHT, compute_ws=True, **RUN_KW)


# ── 로더 ─────────────────────────────────────────────────────────────────────


def test_latest_fuel_costs_picks_latest_month_and_converts() -> None:
    rows = [
        {"month": "2026-08", "fuel": "lng", "cost_won_per_kwh": 150.0},
        {"month": "2026-09", "fuel": "lng", "cost_won_per_kwh": 156.6},
        {"month": "2026-09", "fuel": "oil", "cost_won_per_kwh": None},
    ]
    assert units.latest_fuel_costs(rows) == {"lng": pytest.approx(156_600.0)}


def test_load_units_reads_generators_yaml() -> None:
    costs = {f: 1000.0 for f in ("nuclear", "bituminous_coal", "anthracite", "oil", "lng")}
    fleet = units.load_units(fuel_costs=costs)
    assert len(fleet) == len({u.id for u in fleet})
    hydro = [u for u in fleet if u.fuel == "hydro"]
    assert hydro and all(u.fuel_cost_krw_per_mwh == 0 for u in hydro)  # yaml 명시값 우선
    assert all(u.p_min_mw <= u.p_max_mw for u in fleet)


def test_load_units_subset_and_missing_cost() -> None:
    picked = units.load_units(fuel_costs={"lng": 1.0}, ids=["lng_1", "hydro_1"])
    assert [u.id for u in picked] == ["lng_1", "hydro_1"]
    with pytest.raises(KeyError):
        units.load_units(fuel_costs={}, ids=["lng_1"])
    with pytest.raises(ValueError):
        units.load_units(fuel_costs={}, ids=["없는_유닛"])


def test_fetch_fuel_costs_falls_back_to_csv(monkeypatch) -> None:
    monkeypatch.setenv("COLLECTOR_URL", "http://127.0.0.1:9")  # 닫힌 포트
    costs = units.fetch_fuel_costs(timeout_s=0.5)
    assert costs["lng"] > costs["bituminous_coal"] > costs["nuclear"] > 0


# ── 모델 성질 ────────────────────────────────────────────────────────────────


def test_ws_le_rp_le_eev(result) -> None:
    v = result.vss
    assert result.status == "optimal"
    assert v.ws_won <= v.rp_won + 1 <= v.eev_won + 2
    assert v.vss_won == pytest.approx(v.eev_won - v.rp_won)
    assert v.evpi_won == pytest.approx(v.rp_won - v.ws_won)


def test_expected_cost_matches_rp(result) -> None:
    assert result.expected_cost.total_won == pytest.approx(result.vss.rp_won, rel=1e-6)


def test_balance_holds_in_every_scenario(params) -> None:
    m = stochastic.build_model(FLEET, SCENARIOS, params, 0.10, 1e7)
    sol = stochastic.solve(m, TIGHT)
    for s, sc in enumerate(SCENARIOS):
        supply = sol.second["p"][:, :, s].sum(axis=0) + sol.second["shed"][:, s]
        net = np.array(sc.demand_mw) - np.array(sc.wind_mw)
        assert (supply >= net - 1e-4).all()  # S1 은 부등호


def test_output_follows_first_stage_commitment(params) -> None:
    # S2: 2단계 출력은 1단계 가동 상태 u 안에서만 움직인다 (실시간 추가 기동 없음)
    sol = stochastic.solve(stochastic.build_model(FLEET, SCENARIOS, params, 0.10, 1e7), TIGHT)
    for g, unit_ in enumerate(FLEET):
        on = sol.first["u"][g][:, None]
        p = sol.second["p"][g]
        assert (p <= unit_.p_max_mw * on + 1e-6).all()
        assert (p >= unit_.p_min_mw * on - 1e-6).all()


def test_ramp_limits_hourly_change(params) -> None:
    # S3: |P[t] - P[t-1]| <= RampRate. 기저 유닛(램프 100)이 시나리오마다 지킨다
    sol = stochastic.solve(stochastic.build_model(FLEET, SCENARIOS, params, 0.10, 1e7), TIGHT)
    assert (np.abs(np.diff(sol.second["p"][0], axis=0)) <= 100 + 1e-6).all()


def test_objective_is_startup_fuel_and_shed(result) -> None:
    # 3장 목적함수: 기동비 + 연료비 + 부하차단. 예비력이 충분하면 가정 항(페널티)은 0
    c = result.expected_cost
    assert c.recourse_won == pytest.approx(0.0, abs=1e-6)
    assert c.total_won == pytest.approx(c.startup_won + c.fuel_won + c.shed_won)


def test_fixed_first_stage_is_respected(params) -> None:
    ev = stochastic.solve(
        stochastic.build_model(FLEET, [stochastic.mean_scenario(SCENARIOS)], params, 0.1, 1e7),
        TIGHT,
    )
    eev = stochastic.solve(
        stochastic.build_model(FLEET, SCENARIOS, params, 0.1, 1e7, fixed=ev.first), TIGHT
    )
    for name in ("u", "y", "z"):
        np.testing.assert_array_equal(eev.first[name], ev.first[name])


def test_min_up_time_from_initial_state(params) -> None:
    # 기저 유닛은 초기 가동 6h 를 이미 채웠고, mid 는 기동하면 6h 유지해야 한다
    m = stochastic.build_model(FLEET, SCENARIOS, params, 0.10, 1e7)
    sol = stochastic.solve(m, TIGHT)
    u_mid = sol.first["u"][1]
    starts = [t for t in range(1, len(u_mid)) if u_mid[t] > u_mid[t - 1]]
    for t in starts:
        assert u_mid[t:t + 6].all() or t + 6 > len(u_mid) and u_mid[t:].all()


def test_shed_when_capacity_short(params) -> None:
    # 순부하 1950. 0~4시는 base+LNG = 1200, 5시에야 mid 가 켜져 1600 → 750x5 + 350 부족
    huge = [scen("x", 1.0, [2000.0] * 6)]
    r = stochastic.run(FLEET, huge, params, START, opts=TIGHT, **RUN_KW)
    assert r.scenarios[0].shed_mwh == pytest.approx(750 * 5 + 350, rel=1e-6)
    assert r.expected_hours[0].shed_mw == pytest.approx(750, rel=1e-6)


RT_PARAMS = dataclasses.replace(PARAMS, startup_time_h={"lng": 2, "bituminous_coal": 6})


def test_intraday_start_rules() -> None:
    # R1: 원자력은 당일 기동 불가, LNG 는 2시부터, 석탄은 6h 라 6시간 계획에서는 불가
    sol = stochastic.solve(
        stochastic.build_model(FLEET, SCENARIOS, RT_PARAMS, 0.0, 1e7), TIGHT
    )
    urt, u = sol.second["urt"], sol.first["u"]
    assert not urt[0].any() and not urt[1].any()
    assert not urt[2:, :2].any()
    # R2: 1단계에서 켠 유닛은 당일 또 켜지 않는다
    assert (u[:, :, None] + urt <= 1).all()
    # S2: 출력은 실제 가동 상태 o = u + u^RT 안에서만
    for g, unit_ in enumerate(FLEET):
        o = u[g][:, None] + urt[g]
        assert (sol.second["p"][g] <= unit_.p_max_mw * o + 1e-6).all()
    # R3: 당일 기동이 있으면 기동비 변수가 켜진다
    starts = np.diff(np.concatenate([np.zeros_like(urt[:, :1]), urt], axis=1), axis=1) > 0
    assert (sol.second["yrt"][starts] == 1).all()


def test_intraday_start_lowers_or_keeps_rp() -> None:
    # 2단계 선택지가 늘면 RP 는 같거나 줄어든다
    base = stochastic.solve(
        stochastic.build_model(FLEET, SCENARIOS, PARAMS, 0.0, 1e7), TIGHT
    )
    rt = stochastic.solve(stochastic.build_model(FLEET, SCENARIOS, RT_PARAMS, 0.0, 1e7), TIGHT)
    assert rt.objective <= base.objective + 1


def test_mean_scenario_weights() -> None:
    m = stochastic.mean_scenario(SCENARIOS)
    assert m.demand_mw[0] == pytest.approx(0.3 * 700 + 0.4 * 800 + 0.3 * 900)


def test_load_params_reads_yaml() -> None:
    p = stochastic.load_params()
    assert p.reserve_short_penalty_krw_per_mw > 0
    assert p.startup_time_h["lng"] == 2 and "nuclear" not in p.startup_time_h
    assert p.rt_start_hour(unit("x", "lng", 1, 0, 0)) == 2
    assert p.rt_start_hour(unit("y", "hydro", 1, 0, 0)) == 1  # ceil(0.1)


def test_status_reports_non_optimal_ev(params, monkeypatch) -> None:
    """RP 가 optimal 이어도 EV 가 시간 제한에 걸리면 VSS 를 믿을 수 없으니 status 로 드러낸다."""
    real_solve = stochastic.solve
    calls = []

    def solve(m, opts):
        sol = real_solve(m, opts)
        calls.append(sol)
        return dataclasses.replace(sol, status="time_limit") if len(calls) == 2 else sol

    monkeypatch.setattr(stochastic, "solve", solve)
    res = stochastic.run(
        FLEET, SCENARIOS, params, START, opts=TIGHT, compute_ws=False, **RUN_KW
    )
    assert calls[0].status == "optimal"
    assert res.status == "time_limit"


def test_response_shape(result) -> None:
    assert len(result.first_stage) == len(FLEET) * 6
    assert len(result.scenarios) == 3 and result.n_scenarios == 3
    assert sum(s.probability for s in result.scenarios) == pytest.approx(1.0)
    assert result.scenarios[0].hours is None


# ── API ──────────────────────────────────────────────────────────────────────


def test_api_stochastic(monkeypatch) -> None:
    monkeypatch.setenv("COLLECTOR_URL", "http://127.0.0.1:9")
    main = _load("main")
    body = {
        "start": "2026-09-22T00:00:00+09:00",
        "scenarios": [s.model_dump() for s in SCENARIOS],
        "generator_ids": ["lng_1", "lng_2", "lng_3", "hydro_1"],
        "include_scenario_hours": True,
        "solver": {"time_limit_s": 20, "mip_gap": 0.001},
    }
    r = TestClient(main.app).post("/api/v1/dispatch/stochastic", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["n_scenarios"] == 3 and len(data["scenarios"][0]["hours"]) == 6
    assert {"rp_won", "eev_won", "vss_pct", "target_met"} <= data["vss"].keys()


@pytest.mark.parametrize(("seed", "expected"), [(0, 0), (7, 7), (None, 42)])
def test_api_generated_scenarios_use_given_seed(seed, expected, result, monkeypatch) -> None:
    main = _load("main")
    real_generate, seen = main.generate, []

    def generate(req):
        seen.append(req.seed)
        return real_generate(req)

    monkeypatch.setattr(main, "generate", generate)
    monkeypatch.setattr(main, "load_units", lambda ids=None: FLEET)
    monkeypatch.setattr(main.stochastic, "run", lambda *a, **k: result)
    r = TestClient(main.app).post(
        "/api/v1/dispatch/stochastic", json={"start": "2026-09-22T00:00:00", "seed": seed}
    )
    assert r.status_code == 200, r.text
    assert seen == [expected]


def test_api_rejects_ragged_scenarios() -> None:
    main = _load("main")
    bad = [SCENARIOS[0].model_dump(), {**SCENARIOS[1].model_dump(), "demand_mw": [1.0]}]
    r = TestClient(main.app).post(
        "/api/v1/dispatch/stochastic", json={"start": "2026-09-22T00:00:00", "scenarios": bad}
    )
    assert r.status_code == 422


# ── 미리 계산 (precompute.py) ────────────────────────────────────────────────


def test_precompute_config_reads_yaml(tmp_path, monkeypatch) -> None:
    import yaml

    precompute = _load("precompute")
    raw = units.read_stochastic_yaml()
    raw["precompute"]["seed"]["value"] = 7
    raw["precompute"]["reserve_ratio"]["value"] = 0.0
    raw["precompute"]["unknown"] = 1
    f = tmp_path / "stochastic.yaml"
    f.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    monkeypatch.delenv("PRECOMPUTE_ENABLED", raising=False)
    cfg = precompute.load_config(f)
    assert cfg.seed == 7 and cfg.reserve_ratio == 0.0 and cfg.enabled
    monkeypatch.setenv("PRECOMPUTE_ENABLED", "false")
    assert not precompute.load_config(f).enabled


def test_precompute_repo_config_matches_measurement_table() -> None:
    cfg = _load("precompute").load_config()
    assert (cfg.seed, cfg.n_scenarios, cfg.reserve_ratio) == (1, 10, 0.10)


def test_precompute_compute_once_done_then_error_keeps_result(result, monkeypatch) -> None:
    precompute = _load("precompute")
    store = precompute.LatestStore(dataclasses.replace(precompute.load_config(), enabled=True))
    assert store.snapshot().state == "pending"
    monkeypatch.setattr(precompute, "load_units", lambda: FLEET)
    monkeypatch.setattr(precompute.stochastic, "run", lambda *a, **k: result)
    precompute.compute_once(store, now=datetime(2026, 10, 23, 15, 30))
    snap = store.snapshot()
    assert snap.state == "done" and snap.result.vss.vss_pct == result.vss.vss_pct
    assert snap.computed_at is not None

    def boom(*a, **k):
        raise RuntimeError("RP 를 풀지 못했다")

    monkeypatch.setattr(precompute.stochastic, "run", boom)
    precompute.compute_once(store)
    snap = store.snapshot()
    assert snap.state == "error" and "RP" in snap.error
    assert snap.result is not None  # 이전 결과는 남는다


def test_precompute_disabled_starts_no_thread() -> None:
    import threading

    precompute = _load("precompute")
    store = precompute.LatestStore(dataclasses.replace(precompute.load_config(), enabled=False))
    assert precompute.start_background(store, threading.Event()) is None
    assert store.snapshot().state == "disabled"


def test_api_stochastic_latest(monkeypatch) -> None:
    monkeypatch.setenv("PRECOMPUTE_ENABLED", "false")
    main = _load("main")
    r = TestClient(main.app).get("/api/v1/dispatch/stochastic/latest")
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == "disabled" and data["result"] is None
    assert data["seed"] == 1 and data["reserve_ratio"] == 0.10


# ── config/stochastic.yaml ({value, unit, source}) ──────────────────────────


def test_cfg_value_checks_unit() -> None:
    node = {"value": 60, "unit": "s", "source": "x"}
    assert units.cfg_value(node, "s") == 60
    with pytest.raises(ValueError):
        units.cfg_value(node, "min")
    with pytest.raises(ValueError):
        units.cfg_value(60, "s")  # 맨 숫자는 받지 않는다


def test_api_fills_empty_fields_from_config(result, monkeypatch) -> None:
    main = _load("main")
    d = stochastic.load_request_defaults()
    seen = {}

    def run(*a, **k):
        seen.update(k)
        return result

    monkeypatch.setattr(main, "load_units", lambda ids=None: FLEET)
    monkeypatch.setattr(main.stochastic, "run", run)
    r = TestClient(main.app).post(
        "/api/v1/dispatch/stochastic", json={"start": "2026-09-22T00:00:00"}
    )
    assert r.status_code == 200, r.text
    assert seen["reserve_ratio"] == d.reserve_ratio
    assert seen["shed_penalty"] == d.shed_penalty_won_per_mwh
    assert (seen["opts"].time_limit_s, seen["opts"].mip_gap) == (d.time_limit_s, d.mip_gap)
