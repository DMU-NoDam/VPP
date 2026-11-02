"""MILP(UC + ED) 단위 테스트.

중점
  1. 손계산이 가능한 작은 문제에서 비용·급전이 정확히 맞는지
  2. 필수 제약이 **하나씩** 실제로 구속하는지 (제약을 지웠다면 통과하지 못할 테스트)
  3. infeasible 을 숨기지 않는지
"""

from __future__ import annotations

import pytest

import milp
import validation
from models import HydroPolicy

SOLVER = milp.SolverConfig(name="HiGHS", msg=False)


def solve(inp, **kwargs):
    return milp.solve(inp, solver_config=SOLVER, **kwargs)


# ── 1. 손계산 ────────────────────────────────────────────────────────────────


def test_tiny_case_matches_hand_calculation(make_generator, make_input):
    """싼 유닛 1기로 충분하면 비싼 유닛은 켜지 않는다.

    손계산: 수요 50 MW x 2h 를 cheap(10,000 원/MWh) 이 혼자 담당.
      용량 100 MW >= (1+0.1) x 50 = 55 MW 이므로 예비율도 cheap 혼자 만족.
      비용 = 50 MWh x 2h x 10,000 = 1,000,000 원, 기동비 0.
    """
    cheap = make_generator(gen_id="cheap", fuel_cost_krw_per_mwh=10_000)
    pricey = make_generator(
        gen_id="pricey", fuel_cost_krw_per_mwh=50_000,
        startup_cost_krw=1_000_000, u_init=0,
    )
    result = solve(make_input([cheap, pricey], [50.0, 50.0]))

    assert result.status == "optimal"
    assert result.total_cost_krw == pytest.approx(1_000_000)
    assert result.startup_cost_krw == 0
    by_id = {u.unit_id: u for u in result.units}
    assert by_id["cheap"].p_mw == pytest.approx([50.0, 50.0])
    assert by_id["pricey"].on == [False, False]
    assert validation.all_violations(result, [cheap, pricey]) == []


def test_startup_cost_is_in_objective(make_generator, make_input):
    """기동비가 목적함수에 들어간다.

    손계산: 수요 150 MW -> cheap 100 + pricey 50.
      연료비 100x10,000 + 50x50,000 = 3,500,000, 기동비 1,000,000.
    """
    cheap = make_generator(gen_id="cheap", fuel_cost_krw_per_mwh=10_000)
    pricey = make_generator(
        gen_id="pricey", fuel_cost_krw_per_mwh=50_000,
        startup_cost_krw=1_000_000, u_init=0,
    )
    result = solve(make_input([cheap, pricey], [150.0], reserve_margin_ratio=0.0))

    assert result.status == "optimal"
    assert result.fuel_cost_krw == pytest.approx(3_500_000)
    assert result.startup_cost_krw == pytest.approx(1_000_000)
    assert result.total_cost_krw == pytest.approx(4_500_000)


# ── 2. 제약별 구속 ───────────────────────────────────────────────────────────


def test_power_balance_is_exact(make_generator, make_input):
    gens = [make_generator(gen_id=f"g{i}", p_max_mw=60.0) for i in range(3)]
    result = solve(make_input(gens, [50.0, 120.0, 95.0]))

    assert result.status == "optimal"
    for t, demand in enumerate(result.net_demand_mw):
        assert result.supply_mw(t) == pytest.approx(demand, abs=1e-6)


def test_reserve_forces_extra_commitment(make_generator, make_input):
    """예비율 때문에, 수요만 보면 필요 없는 유닛이 켜진다.

    수요 95 MW 는 cheap(100 MW) 혼자 담당할 수 있지만,
    (1+0.1) x 95 = 104.5 MW > 100 MW 이므로 pricey 도 켜야 한다.
    손계산: cheap 75 + pricey 20(=p_min) -> 750,000 + 1,000,000 + 기동비 1,000,000.
    """
    cheap = make_generator(gen_id="cheap", fuel_cost_krw_per_mwh=10_000)
    pricey = make_generator(
        gen_id="pricey", fuel_cost_krw_per_mwh=50_000,
        startup_cost_krw=1_000_000, u_init=0,
    )
    result = solve(make_input([cheap, pricey], [95.0]))

    assert result.status == "optimal"
    by_id = {u.unit_id: u for u in result.units}
    assert by_id["pricey"].on == [True]
    assert by_id["pricey"].p_mw[0] == pytest.approx(20.0)
    assert by_id["cheap"].p_mw[0] == pytest.approx(75.0)
    assert result.total_cost_krw == pytest.approx(2_750_000)
    assert result.reserve_pct(0) >= 10.0


def test_reserve_counts_only_committed_capacity(make_generator, make_input):
    """정지한 유닛의 용량은 예비력으로 치지 않는다."""
    gens = [make_generator(gen_id=f"g{i}", p_max_mw=100.0, u_init=0) for i in range(4)]
    result = solve(make_input(gens, [150.0]))

    assert result.status == "optimal"
    committed_capacity = result.available_capacity_mw(0)
    assert committed_capacity >= 1.1 * 150.0 - 1e-6
    assert validation.check_reserve(result) == []


def test_p_min_is_enforced_when_on(make_generator, make_input):
    """가동 중이면 p_min 이상이어야 한다 (과잉발전이 생기면 수급 균형이 깨진다)."""
    big = make_generator(gen_id="big", p_max_mw=100.0, p_min_mw=40.0)
    small = make_generator(
        gen_id="small", p_max_mw=100.0, p_min_mw=40.0,
        fuel_cost_krw_per_mwh=20_000, u_init=0,
    )
    result = solve(make_input([big, small], [45.0]))

    assert result.status == "optimal"
    for unit in result.units:
        if unit.on[0]:
            assert unit.p_mw[0] >= 40.0 - 1e-6
    assert validation.check_capacity(result, [big, small]) == []


def test_ramp_up_limits_output_growth(make_generator, make_input):
    """램프업: 직전 출력 + ramp_up 을 넘을 수 없다.

    p_init 50 MW, ramp_up 10 MW/h 인 유닛만 있으면 t=0 에 60 MW 가 상한이다.
    수요 70 MW 는 두 번째 유닛이 켜져야 채워진다.
    """
    slow = make_generator(
        gen_id="slow", p_max_mw=100.0, p_min_mw=10.0,
        ramp_up_mw_per_h=10.0, ramp_down_mw_per_h=10.0,
    )
    fast = make_generator(
        gen_id="fast", p_max_mw=100.0, p_min_mw=10.0,
        fuel_cost_krw_per_mwh=90_000, u_init=0,
    )
    inp = make_input([slow, fast], [70.0], reserve_margin_ratio=0.0,
                     p_init_mw={"slow": 50.0, "fast": 0.0})
    result = solve(inp)

    assert result.status == "optimal"
    by_id = {u.unit_id: u for u in result.units}
    assert by_id["slow"].p_mw[0] == pytest.approx(60.0)
    assert by_id["fast"].p_mw[0] == pytest.approx(10.0)
    assert validation.check_ramp(result, [slow, fast], inp.p_init_mw) == []


def test_ramp_down_limits_output_drop(make_generator, make_input):
    """램프다운: 수요가 급락해도 직전 출력에서 ramp_down 이상 못 내린다.

    p_init 80, ramp_down 10 -> t=0 에 70 MW 가 하한. 수요 40 MW 를 맞추려면
    과잉발전이 되므로 MILP 는 못 푼다 (슬랙 없이는 infeasible).
    """
    slow = make_generator(
        gen_id="slow", p_max_mw=100.0, p_min_mw=10.0,
        ramp_up_mw_per_h=10.0, ramp_down_mw_per_h=10.0,
    )
    inp = make_input([slow], [40.0], reserve_margin_ratio=0.0, p_init_mw={"slow": 80.0})

    assert solve(inp).status == "infeasible"


def test_initial_state_forces_min_up_time(make_generator, make_input):
    """min_up 을 못 채운 유닛은 비싸도 계속 돌아야 한다.

    pricey: min_up 4h, 켜진 지 1h -> t=0,1,2 는 가동 고정. p_min 20 MW 를 낸다.
    """
    cheap = make_generator(gen_id="cheap", p_max_mw=200.0, fuel_cost_krw_per_mwh=10_000)
    pricey = make_generator(
        gen_id="pricey", fuel_cost_krw_per_mwh=90_000,
        min_up_h=4, u_init=1, init_state_h=1,
    )
    result = solve(make_input([cheap, pricey], [100.0] * 5, reserve_margin_ratio=0.0))

    assert result.status == "optimal"
    by_id = {u.unit_id: u for u in result.units}
    assert by_id["pricey"].on[:3] == [True, True, True]
    assert by_id["pricey"].p_mw[0] == pytest.approx(20.0)
    assert by_id["pricey"].on[3] is False  # 4시간을 채우면 바로 내린다
    assert validation.check_min_up_down(result, [cheap, pricey]) == []


def test_initial_state_forces_min_down_time(make_generator, make_input):
    """min_down 을 못 채운 유닛은 필요해도 못 켠다.

    helper: min_down 3h, 꺼진 지 1h -> t=0,1 기동 금지. 그래서 t=0 수요는
    main 혼자 감당해야 한다.
    """
    main_unit = make_generator(gen_id="main", p_max_mw=100.0)
    helper = make_generator(
        gen_id="helper", p_max_mw=100.0, min_down_h=3, u_init=0, init_state_h=1,
    )
    result = solve(make_input([main_unit, helper], [90.0, 90.0, 150.0],
                              reserve_margin_ratio=0.0))

    assert result.status == "optimal"
    by_id = {u.unit_id: u for u in result.units}
    assert by_id["helper"].on[:2] == [False, False]
    assert by_id["helper"].on[2] is True
    assert validation.check_min_up_down(result, [main_unit, helper]) == []


def test_min_down_blocks_infeasible_demand(make_generator, make_input):
    """정지 제약 때문에 용량이 모자라면 infeasible 이다 (조용히 넘기지 않는다)."""
    main_unit = make_generator(gen_id="main", p_max_mw=100.0)
    helper = make_generator(
        gen_id="helper", p_max_mw=100.0, min_down_h=3, u_init=0, init_state_h=1,
    )
    inp = make_input([main_unit, helper], [150.0], reserve_margin_ratio=0.0)

    assert solve(inp).status == "infeasible"


def test_hydro_limit_triggers_below_threshold(make_generator, make_input):
    """저수율 < 30% 이면 수력 상한이 줄어든다.

    저수율 15%, linear(min_factor 0.5) -> factor 0.5 + 0.5x15/30 = 0.75.
    수력 상한 100 -> 75 MW. 나머지는 비싼 유닛이 메운다.
    """
    hydro = make_generator(
        gen_id="hydro_1", fuel="hydro", p_max_mw=100.0, p_min_mw=0.0,
        fuel_cost_krw_per_mwh=0.0,
    )
    lng = make_generator(gen_id="lng_1", p_max_mw=100.0, p_min_mw=0.0,
                         fuel_cost_krw_per_mwh=100_000)
    inp = make_input([hydro, lng], [90.0], reserve_margin_ratio=0.0, storage_rate_pct=15.0)
    result = solve(inp, hydro_policy=HydroPolicy())

    assert result.status == "optimal"
    assert result.hydro_factor == pytest.approx(0.75)
    by_id = {u.unit_id: u for u in result.units}
    assert by_id["hydro_1"].p_max_eff_mw == pytest.approx(75.0)
    assert by_id["hydro_1"].p_mw[0] == pytest.approx(75.0)
    assert by_id["lng_1"].p_mw[0] == pytest.approx(15.0)
    assert validation.check_hydro_limit(result, [hydro, lng]) == []


def test_hydro_not_limited_above_threshold(make_generator, make_input):
    hydro = make_generator(
        gen_id="hydro_1", fuel="hydro", p_max_mw=100.0, p_min_mw=0.0,
        fuel_cost_krw_per_mwh=0.0,
    )
    inp = make_input([hydro], [90.0], reserve_margin_ratio=0.0, storage_rate_pct=45.0)
    result = solve(inp)

    assert result.hydro_factor == 1.0
    assert result.units[0].p_mw[0] == pytest.approx(90.0)


def test_hydro_fixed_mode(make_generator, make_input):
    """fixed 모드는 임계값 미만에서 일괄 min_factor 를 적용한다."""
    hydro = make_generator(
        gen_id="hydro_1", fuel="hydro", p_max_mw=100.0, p_min_mw=0.0,
        fuel_cost_krw_per_mwh=0.0,
    )
    lng = make_generator(gen_id="lng_1", p_max_mw=100.0, p_min_mw=0.0,
                         fuel_cost_krw_per_mwh=100_000)
    inp = make_input([hydro, lng], [90.0], reserve_margin_ratio=0.0, storage_rate_pct=29.0)
    result = solve(inp, hydro_policy=HydroPolicy(mode="fixed", min_factor=0.4))

    assert result.hydro_factor == pytest.approx(0.4)
    assert result.units[0].p_mw[0] == pytest.approx(40.0)


# ── 3. infeasible 과 슬랙 ────────────────────────────────────────────────────


def test_capacity_shortage_is_infeasible(make_generator, make_input):
    """용량 부족은 infeasible 로 드러난다 (성공 응답을 만들지 않는다)."""
    gen = make_generator(gen_id="only", p_max_mw=100.0)
    result = solve(make_input([gen], [200.0]))

    assert result.status == "infeasible"
    assert result.total_cost_krw == 0
    assert any("solver status" in note for note in result.notes)


def test_load_shedding_quantifies_shortage(make_generator, make_input):
    """슬랙을 켜면 부족량이 숫자로 나오고, 비용에 페널티가 잡힌다."""
    gen = make_generator(gen_id="only", p_max_mw=100.0, p_min_mw=0.0)
    result = solve(
        make_input([gen], [150.0], reserve_margin_ratio=0.0),
        allow_load_shedding=True,
        shed_penalty_krw_per_mwh=1_000_000,
    )

    assert result.status == "optimal"
    assert result.shed_mw[0] == pytest.approx(50.0)
    assert result.shed_cost_krw == pytest.approx(50 * 1_000_000)
    assert any("부하차단" in note for note in result.notes)


def test_shed_is_zero_when_not_allowed(make_generator, make_input):
    """기본값에서는 슬랙이 0 으로 묶여 있다."""
    gen = make_generator(gen_id="only", p_max_mw=100.0)
    result = solve(make_input([gen], [90.0], reserve_margin_ratio=0.0))

    assert result.shed_mw == [0.0]
    assert result.shed_cost_krw == 0


# ── 4. 모델 메타데이터 ───────────────────────────────────────────────────────


def test_model_size_is_reported(make_generator, make_input):
    """10초 KPI 추적을 위해 문제 크기와 시간이 남는다."""
    gens = [make_generator(gen_id=f"g{i}") for i in range(3)]
    result = solve(make_input(gens, [100.0] * 24))

    size = result.size
    assert size is not None
    assert size.n_binaries == 3 * 24           # u[g,t] 만 이진
    assert size.n_variables == 3 * 24 * 4 + 24  # u,v,w,p + shed
    assert size.n_constraints > 0
    assert size.total_time_s > 0


def test_unknown_solver_is_rejected(make_generator, make_input):
    gen = make_generator()
    with pytest.raises(milp.SolverUnavailableError, match="HiGHS"):
        milp.solve(
            make_input([gen], [50.0]),
            solver_config=milp.SolverConfig(name="GUROBI"),
        )
