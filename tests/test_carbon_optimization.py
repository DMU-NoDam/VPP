"""탄소 제약은 공급을 유지하면서 발전 믹스를 바꿔야 한다."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import carbon
import main
import milp
import pareto
import service
import validation
from models import HydroPolicy
from shared.schemas import ParetoRequest


@pytest.fixture
def two_fuel(make_generator, make_input):
    coal = make_generator(gen_id="coal", fuel="bituminous_coal", p_min_mw=0,
                          co2_ton_per_mwh=1, fuel_cost_krw_per_mwh=10_000)
    gas = make_generator(gen_id="gas", fuel="lng", p_min_mw=0,
                         co2_ton_per_mwh=0.5, fuel_cost_krw_per_mwh=20_000)
    return make_input([coal, gas], [100], reserve_margin_ratio=0)


def test_cap_changes_mix_and_preserves_cost_accounting(two_fuel):
    original = milp.solve(two_fuel)
    capped = milp.solve(two_fuel, carbon_cap_ton=75)
    assert carbon.total_ton(original.units) == pytest.approx(100)
    assert carbon.total_ton(capped.units) == pytest.approx(75)
    assert [u.energy_mwh for u in capped.units] == pytest.approx([50, 50])
    assert capped.total_cost_krw == pytest.approx(1_500_000)
    assert capped.total_cost_krw == capped.operating_cost_krw
    assert sum(capped.shed_mw) == 0
    assert all(c.ok for c in validation.report(capped, two_fuel.generators))


def test_default_regression_and_impossible_cap(two_fuel):
    original = milp.solve(two_fuel)
    explicit = milp.solve(two_fuel, carbon_cap_ton=None)
    assert original.total_cost_krw == explicit.total_cost_krw
    assert original.units == explicit.units
    failed = milp.solve(two_fuel, carbon_cap_ton=49)
    assert failed.status == "infeasible"
    assert not failed.has_solution
    with pytest.raises(ValueError, match="부하차단"):
        milp.solve(two_fuel, carbon_cap_ton=75, allow_load_shedding=True)


def test_carbon_objective_uses_cleaner_generation(two_fuel):
    result = milp.solve(two_fuel, objective_mode="carbon")
    assert result.status == "optimal"
    assert carbon.total_ton(result.units) == pytest.approx(50)
    assert result.operating_cost_krw == pytest.approx(2_000_000)


def test_zero_cap_and_zero_emission_fleet(two_fuel):
    clean = replace(two_fuel.generators[0], co2_ton_per_mwh=0)
    result = milp.solve(replace(two_fuel, generators=[clean]), carbon_cap_ton=0,
                        objective_mode="carbon")
    assert result.has_solution
    assert carbon.total_ton(result.units) == 0


def test_pareto_endpoints_monotonicity_and_target(two_fuel):
    sweep = pareto.run(two_fuel, solver_config=milp.SolverConfig(), hydro_policy=HydroPolicy(),
                       ess=None, points=5, total_time_limit_s=10,
                       emission_tolerance_ton=0.0001, reduction_targets_pct=[60])
    assert sweep.complete
    assert sweep.minimum_emissions_ton == pytest.approx(50)
    assert sweep.attempts[-1].result.status == "infeasible"
    capped = sorted([a for a in sweep.attempts if a.cap_ton is not None
                     and a.result.has_solution], key=lambda a: a.cap_ton, reverse=True)
    costs = [a.result.operating_cost_krw for a in capped]
    assert costs == sorted(costs)


def test_ess_capped_dispatch_preserves_energy(two_fuel, make_ess):
    ess = make_ess(soc_final_min_pct=50)
    result = milp.solve(two_fuel, carbon_cap_ton=75, ess=ess)
    assert result.status == "optimal"
    assert result.ess.soc_mwh[-1] >= ess.soc_init_mwh - 1e-6
    assert carbon.total_ton(result.units) <= 75.0001
    assert all(c.ok for c in validation.report(result, two_fuel.generators))


@pytest.fixture
def toy_api(monkeypatch, two_fuel):
    monkeypatch.setattr(service, "generators", lambda cfg: list(two_fuel.generators))
    return TestClient(main.app)


def test_cap_api_and_validation(toy_api):
    body = {"horizon_h": 1, "net_demand_mw": [100], "reserve_ratio": 0,
            "use_ess": False, "carbon_cap_ton": 75}
    response = toy_api.post("/api/v1/dispatch/milp", json=body)
    assert response.status_code == 200
    data = response.json()
    assert data["carbon"]["cap_binding"]
    assert data["carbon"]["valid"]
    assert all(c["ok"] for c in data["constraints"])
    assert toy_api.post("/api/v1/dispatch/milp", json={
        **body, "carbon_cap_ton": -1}).status_code == 422
    assert toy_api.post("/api/v1/dispatch/milp", json={
        **body, "allow_load_shedding": True}).status_code == 422
    failed = toy_api.post("/api/v1/dispatch/milp", json={**body, "carbon_cap_ton": 49}).json()
    assert not failed["carbon"]["valid"]
    assert failed["carbon"]["cap_binding"] is None


def test_pareto_api_comparison_and_failures(toy_api):
    body = {"horizon_h": 1, "net_demand_mw": [100], "reserve_ratio": 0,
            "use_ess": False, "carbon_accounting": "configured", "points": 3,
            "reduction_targets_pct": [25, 60]}
    response = toy_api.post("/api/v1/dispatch/pareto", json=body)
    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "complete"
    assert data["maximum_reduction_pct"] == pytest.approx(50)
    target = next(p for p in data["points"] if p["label"] == "target_25pct")
    assert target["reduction_pct"] == pytest.approx(25)
    assert target["cost_increase_pct"] == pytest.approx(50)
    assert target["fuel_energy_mwh"]["bituminous_coal"] == pytest.approx(50)
    failed = next(p for p in data["points"] if p["label"] == "target_60pct")
    assert not failed["valid"] and failed["carbon_ton"] is None
    assert failed["operating_cost_krw"] is None
    assert len(data["frontier"]) == 3
    for updates in ({"allow_load_shedding": True}, {"carbon_cap_ton": 75},
                    {"reduction_targets_pct": [101]}, {"points": 1}):
        assert toy_api.post("/api/v1/dispatch/pareto", json={**body, **updates}).status_code == 422


def test_lifecycle_is_explicit_and_baseline_unchanged(toy_api):
    req = ParetoRequest(horizon_h=1, net_demand_mw=[100], reserve_ratio=0, use_ess=False, points=2)
    result = service.run_pareto(req)
    assert result.factors == {"bituminous_coal": 0.923, "lng": 0.458}
    assert result.accounting_basis == "dispatchable_lifecycle_midpoint"
    assert result.reference.carbon_ton == pytest.approx(92.3)


def test_expired_budget_does_not_fabricate_points(two_fuel, monkeypatch):
    clock = iter([0, 2, 2])
    monkeypatch.setattr(pareto.time, "perf_counter", lambda: next(clock))
    sweep = pareto.run(two_fuel, solver_config=milp.SolverConfig(), hydro_policy=HydroPolicy(),
                       ess=None, points=5, total_time_limit_s=1,
                       emission_tolerance_ton=0.0001, reduction_targets_pct=[])
    assert not sweep.complete and sweep.reference is None and sweep.attempts == []


def test_solver_timeout_without_incumbent_is_not_a_zero_emission_plan(two_fuel, monkeypatch):
    import pulp

    def no_solution(problem, solver):
        problem.sol_status = pulp.LpSolutionNoSolutionFound
        return pulp.LpStatusNotSolved

    monkeypatch.setattr(pulp.LpProblem, "solve", no_solution)
    result = milp.solve(two_fuel, carbon_cap_ton=75)
    assert not result.has_solution
    assert result.status == "error"


def test_missing_analysis_factor_is_not_assumed_zero(two_fuel):
    from loaders.config_loader import ConfigError

    with pytest.raises(ConfigError, match="계수가 없는"):
        carbon.analysis_fleet(two_fuel.generators, {"factors": {"lng": 0.5}})


def test_minimum_carbon_cost_tiebreak(make_generator, make_input):
    cheap = make_generator(gen_id="cheap", p_min_mw=0, co2_ton_per_mwh=0.5,
                           fuel_cost_krw_per_mwh=10_000)
    expensive = replace(cheap, id="expensive", fuel_cost_krw_per_mwh=20_000)
    inp = make_input([cheap, expensive], [100], reserve_margin_ratio=0)
    sweep = pareto.run(inp, solver_config=milp.SolverConfig(), hydro_policy=HydroPolicy(),
                       ess=None, points=3, total_time_limit_s=10,
                       emission_tolerance_ton=0.0001, reduction_targets_pct=[])
    assert sweep.complete
    assert sweep.minimum.result.operating_cost_krw == pytest.approx(1_000_000)
    assert sweep.minimum_emissions_ton == pytest.approx(50)


def test_zero_gap_is_forwarded_to_solver(monkeypatch):
    calls = []

    class Solver:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def available(self):
            return True

    monkeypatch.setattr(milp.pulp, "HiGHS", Solver)
    milp.build_solver(milp.SolverConfig(mip_gap=0))
    assert calls[0]["gapRel"] == 0
