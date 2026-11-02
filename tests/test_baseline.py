"""Rule-based baseline 단위 테스트.

중점
  1. 규칙이 문서(baseline.py docstring)와 같게 동작하는지
  2. baseline 도 **모든 필수 제약을 만족**하는지 — 위반하는 baseline 과 비교하면
     절감률이 과대평가된다 (CLAUDE.local.md: 일부러 약하게 만들지 않는다)
  3. 절감률 정의식이 맞는지
"""

from __future__ import annotations

import pytest

import baseline
import milp
import schedule
import validation

SOLVER = milp.SolverConfig(name="HiGHS", msg=False)


def test_priority_is_fuel_cost_ascending(make_generator):
    gens = [
        make_generator(gen_id="lng", fuel_cost_krw_per_mwh=150_000),
        make_generator(gen_id="nuclear", fuel_cost_krw_per_mwh=6_500),
        make_generator(gen_id="coal", fuel_cost_krw_per_mwh=90_000),
    ]
    assert [g.id for g in baseline.priority_order(gens)] == ["nuclear", "coal", "lng"]


def test_priority_breaks_ties_with_bigger_unit_first(make_generator):
    gens = [
        make_generator(gen_id="small", p_max_mw=100.0),
        make_generator(gen_id="big", p_max_mw=500.0),
    ]
    assert [g.id for g in baseline.priority_order(gens)] == ["big", "small"]


def test_cheap_unit_is_dispatched_first(make_generator, make_input):
    """R5: 싼 유닛을 상한까지 채운 뒤 비싼 유닛을 쓴다."""
    cheap = make_generator(gen_id="cheap", p_max_mw=100.0, p_min_mw=0.0,
                           fuel_cost_krw_per_mwh=10_000)
    pricey = make_generator(gen_id="pricey", p_max_mw=100.0, p_min_mw=0.0,
                            fuel_cost_krw_per_mwh=90_000)
    result = baseline.run(make_input([cheap, pricey], [120.0], reserve_margin_ratio=0.0))

    by_id = {u.unit_id: u for u in result.units}
    assert by_id["cheap"].p_mw[0] == pytest.approx(100.0)
    assert by_id["pricey"].p_mw[0] == pytest.approx(20.0)


def test_baseline_meets_power_balance_and_reserve(make_generator, make_input):
    gens = [make_generator(gen_id=f"g{i}", p_max_mw=60.0, p_min_mw=10.0, u_init=0)
            for i in range(5)]
    result = baseline.run(make_input(gens, [50.0, 120.0, 200.0, 90.0]))

    assert validation.check_power_balance(result) == []
    assert validation.check_reserve(result) == []


def test_baseline_respects_min_up_down_and_ramp(make_generator, make_input):
    """실데이터 규모에서 baseline 도 제약 위반이 없어야 한다."""
    gens = [
        make_generator(gen_id="base", p_max_mw=200.0, p_min_mw=100.0, min_up_h=6,
                       min_down_h=6, ramp_up_mw_per_h=40.0, ramp_down_mw_per_h=40.0,
                       fuel_cost_krw_per_mwh=6_500),
        make_generator(gen_id="mid", p_max_mw=150.0, p_min_mw=60.0, min_up_h=3,
                       min_down_h=3, ramp_up_mw_per_h=90.0, ramp_down_mw_per_h=90.0,
                       fuel_cost_krw_per_mwh=90_000, u_init=0, init_state_h=6),
        make_generator(gen_id="peak", p_max_mw=100.0, p_min_mw=20.0, min_up_h=2,
                       min_down_h=2, fuel_cost_krw_per_mwh=150_000, u_init=0,
                       init_state_h=6),
    ]
    demand = [180.0, 200.0, 260.0, 320.0, 300.0, 240.0, 190.0, 170.0]
    result = baseline.run(make_input(gens, demand, reserve_margin_ratio=0.10),
                          hydro_policy=None)

    assert validation.all_violations(result, gens) == []


def test_baseline_reports_unserved_demand(make_generator, make_input):
    """규칙으로 못 채우면 숫자로 남긴다 (조용히 균형을 깨지 않는다)."""
    gen = make_generator(gen_id="only", p_max_mw=100.0, p_min_mw=0.0)
    result = baseline.run(make_input([gen], [150.0], reserve_margin_ratio=0.0))

    assert result.shed_mw[0] == pytest.approx(50.0)
    assert any("채우지 못한" in note for note in result.notes)


def test_baseline_applies_hydro_limit(make_generator, make_input):
    hydro = make_generator(gen_id="hydro_1", fuel="hydro", p_max_mw=100.0, p_min_mw=0.0,
                           fuel_cost_krw_per_mwh=0.0)
    lng = make_generator(gen_id="lng_1", p_max_mw=100.0, p_min_mw=0.0,
                         fuel_cost_krw_per_mwh=100_000)
    result = baseline.run(
        make_input([hydro, lng], [90.0], reserve_margin_ratio=0.0, storage_rate_pct=15.0)
    )

    by_id = {u.unit_id: u for u in result.units}
    assert result.hydro_factor == pytest.approx(0.75)
    assert by_id["hydro_1"].p_mw[0] == pytest.approx(75.0)
    assert by_id["lng_1"].p_mw[0] == pytest.approx(15.0)


# ── 비교 ─────────────────────────────────────────────────────────────────────


def test_cost_reduction_formula():
    """(baseline - milp) / baseline x 100."""
    class Fake:
        def __init__(self, cost):
            self.total_cost_krw = cost

    assert schedule.cost_reduction_pct(Fake(100.0), Fake(90.0)) == pytest.approx(10.0)
    assert schedule.cost_reduction_pct(Fake(0.0), Fake(0.0)) == 0.0


def test_milp_is_never_worse_than_baseline(make_generator, make_input):
    """같은 입력·같은 회계에서 MILP 비용은 baseline 이하다.

    둘 중 하나라도 제약을 위반하면 이 비교는 의미가 없으므로 제약 검증도 같이 한다.
    """
    gens = [
        make_generator(gen_id="nuc", p_max_mw=400.0, p_min_mw=220.0, min_up_h=24,
                       min_down_h=24, ramp_up_mw_per_h=40.0, ramp_down_mw_per_h=40.0,
                       fuel_cost_krw_per_mwh=6_500, startup_cost_krw=500_000_000),
        make_generator(gen_id="coal_1", p_max_mw=250.0, p_min_mw=100.0, min_up_h=6,
                       min_down_h=6, ramp_up_mw_per_h=150.0, ramp_down_mw_per_h=150.0,
                       fuel_cost_krw_per_mwh=90_000, startup_cost_krw=120_000_000,
                       u_init=1, init_state_h=12),
        make_generator(gen_id="coal_2", p_max_mw=250.0, p_min_mw=100.0, min_up_h=6,
                       min_down_h=6, ramp_up_mw_per_h=150.0, ramp_down_mw_per_h=150.0,
                       fuel_cost_krw_per_mwh=90_000, startup_cost_krw=120_000_000,
                       u_init=0, init_state_h=12),
        make_generator(gen_id="lng_1", p_max_mw=120.0, p_min_mw=48.0, min_up_h=2,
                       min_down_h=2, fuel_cost_krw_per_mwh=155_000,
                       startup_cost_krw=40_000_000, u_init=0, init_state_h=2),
        make_generator(gen_id="lng_2", p_max_mw=120.0, p_min_mw=48.0, min_up_h=2,
                       min_down_h=2, fuel_cost_krw_per_mwh=155_000,
                       startup_cost_krw=40_000_000, u_init=0, init_state_h=2),
    ]
    demand = [520.0, 500.0, 495.0, 510.0, 560.0, 640.0, 700.0, 760.0,
              800.0, 790.0, 760.0, 720.0, 680.0, 640.0, 600.0, 580.0,
              600.0, 660.0, 720.0, 740.0, 700.0, 640.0, 580.0, 540.0]
    inp = make_input(gens, demand)

    rule = baseline.run(inp, shed_penalty_krw_per_mwh=0.0)
    optimized = milp.solve(inp, solver_config=SOLVER)

    assert optimized.status == "optimal"
    assert validation.all_violations(rule, gens) == []
    assert validation.all_violations(optimized, gens) == []
    assert sum(rule.shed_mw) == 0
    assert optimized.total_cost_krw <= rule.total_cost_krw + 1e-6
    assert schedule.cost_reduction_pct(rule, optimized) >= 0


def test_baseline_reports_surplus_instead_of_breaking_ramp(make_generator, make_input):
    """램프 한계로 감발을 못 하면 과잉발전으로 남는다 (제약을 깨지 않는다).

    base 는 ramp_down 40 MW/h 인데 수요가 60 MW 떨어진다. 직전 출력이 하한을
    만들어 더 못 내리므로, 그 차이가 surplus_mw 에 기록된다.
    """
    base = make_generator(
        gen_id="base", p_max_mw=200.0, p_min_mw=20.0,
        ramp_up_mw_per_h=40.0, ramp_down_mw_per_h=40.0, fuel_cost_krw_per_mwh=6_500,
    )
    result = baseline.run(
        make_input([base], [180.0, 120.0], reserve_margin_ratio=0.0,
                   p_init_mw={"base": 180.0})
    )

    assert result.units[0].p_mw == pytest.approx([180.0, 140.0])
    assert result.surplus_mw == pytest.approx([0.0, 20.0])
    assert validation.check_ramp(result, [base], {"base": 180.0}) == []
    assert validation.check_power_balance(result) == []
    assert any("과잉발전" in note for note in result.notes)
    assert any(c.name == "과잉발전 없음" and not c.ok
               for c in validation.report(result, [base]))


def test_baseline_decommits_to_avoid_overgeneration(make_generator, make_input):
    """투입된 유닛의 최소출력 합이 수요를 넘으면 비싼 유닛을 더 뺀다.

    둘 다 p_min 60 인데 수요가 80 MW 다. 둘 다 돌리면 최소출력 합 120 > 80 이라
    수급 균형을 못 맞춘다. 예비율보다 균형이 우선이므로 비싼 쪽을 뺀다.
    """
    cheap = make_generator(gen_id="cheap", p_max_mw=100.0, p_min_mw=60.0,
                           fuel_cost_krw_per_mwh=10_000)
    pricey = make_generator(gen_id="pricey", p_max_mw=100.0, p_min_mw=60.0,
                            fuel_cost_krw_per_mwh=90_000)
    result = baseline.run(make_input([cheap, pricey], [80.0], reserve_margin_ratio=0.5))

    by_id = {u.unit_id: u for u in result.units}
    assert by_id["cheap"].on == [True]
    assert by_id["pricey"].on == [False]
    assert by_id["cheap"].p_mw[0] == pytest.approx(80.0)
    assert validation.check_power_balance(result) == []
    assert validation.check_capacity(result, [cheap, pricey]) == []


def test_locked_on_unit_is_not_decommitted(make_generator, make_input):
    """최소 기동시간을 못 채운 유닛은 과잉발전 상황에서도 못 뺀다."""
    cheap = make_generator(gen_id="cheap", p_max_mw=100.0, p_min_mw=10.0,
                           fuel_cost_krw_per_mwh=10_000)
    locked = make_generator(gen_id="locked", p_max_mw=100.0, p_min_mw=60.0,
                            fuel_cost_krw_per_mwh=90_000, min_up_h=6, u_init=1,
                            init_state_h=1)
    result = baseline.run(make_input([cheap, locked], [50.0], reserve_margin_ratio=0.0))

    by_id = {u.unit_id: u for u in result.units}
    assert by_id["locked"].on == [True]
    assert by_id["locked"].p_mw[0] >= 60.0 - 1e-6
    assert validation.check_min_up_down(result, [cheap, locked]) == []
