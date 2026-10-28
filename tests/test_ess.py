"""ESS 단위 테스트.

중점
  1. SOC 전이식과 효율이 손계산과 맞는지 (여기서 틀리면 "공짜 에너지"가 생긴다)
  2. 출력·SOC·종료 SOC 제약이 실제로 구속하는지
  3. 동시 충·방전이 생기지 않는지
  4. 피크 감소율 정의식과 peak_shaving 모드
  5. 검사기(validation.check_ess)가 틀린 ESS 스케줄을 잡는지

ESS 기본 사양(make_ess)은 100 MW / 400 MWh, 왕복효율 0.64 (eta 0.8 x 0.8) 로
손계산이 되는 값이다.
"""

from __future__ import annotations

import pytest

import carbon
import milp
import validation
from models import EssSchedule
from schedule import assemble, effective_p_max

SOLVER = milp.SolverConfig(name="HiGHS", msg=False)


def solve(inp, ess=None, **kwargs):
    return milp.solve(inp, solver_config=SOLVER, ess=ess, **kwargs)


@pytest.fixture
def two_price_hours(make_generator, make_input):
    """싼 시간 / 비싼 시간이 생기는 2시간 문제.

    수요 [40, 120], cheap(80 MW, 1만원/MWh) + pricey(100 MW, 10만원/MWh).
      t=0: cheap 40                      -> 40 x 1만   =   40만원
      t=1: cheap 80 + pricey 40          -> 80만 + 400만 = 480만원
      합계 520만원 (ESS 없음)
    """
    cheap = make_generator(gen_id="cheap", p_max_mw=80.0, p_min_mw=0.0,
                           fuel_cost_krw_per_mwh=10_000)
    pricey = make_generator(gen_id="pricey", p_max_mw=100.0, p_min_mw=0.0,
                            fuel_cost_krw_per_mwh=100_000)
    return [cheap, pricey], make_input([cheap, pricey], [40.0, 120.0],
                                       reserve_margin_ratio=0.0)


# ── 1. SOC 전이식과 효율 ─────────────────────────────────────────────────────


def test_arbitrage_matches_hand_calculation(two_price_hours, make_ess):
    """싼 시간에 충전해 비싼 시간에 방전한다. 비용까지 손계산과 맞는다.

    충전 여유는 t=0 의 cheap 잔여 용량 40 MW.
      soc(0) = 200 + 0.8 x 40 = 232 MWh
      t=1 방전 한계 = (232 - 200) x 0.8 = 25.6 MW   (종료 SOC >= 200 MWh)
      비용 = t0: cheap 80 x 1만 = 80만
             t1: cheap 80 x 1만 + pricey (120 - 25.6 - 80) x 10만 = 80만 + 144만
           = 304만원  (ESS 없을 때 520만원)
    """
    gens, inp = two_price_hours
    ess = make_ess(soc_final_min_pct=50.0)

    without = solve(inp)
    with_ess = solve(inp, ess=ess)

    assert without.total_cost_krw == pytest.approx(5_200_000)
    assert with_ess.status == "optimal"
    assert with_ess.ess.charge_mw == pytest.approx([40.0, 0.0])
    assert with_ess.ess.discharge_mw == pytest.approx([0.0, 25.6])
    assert with_ess.ess.soc_mwh == pytest.approx([232.0, 200.0])
    assert with_ess.total_cost_krw == pytest.approx(3_040_000)
    assert validation.all_violations(with_ess, gens) == []


def test_round_trip_loss_is_accounted(two_price_hours, make_ess):
    """충전량 - 방전량 - SOC 증가분 = 손실. 0.64 왕복효율이면 36% 가 사라진다."""
    _, inp = two_price_hours
    result = solve(inp, ess=make_ess(soc_final_min_pct=50.0))

    ess = result.ess
    assert ess.energy_charged_mwh == pytest.approx(40.0)
    assert ess.energy_discharged_mwh == pytest.approx(25.6)
    # 충전 40 -> 저장 32, 방전 25.6 은 저장 32 를 소모. 손실 = 40 - 25.6 = 14.4
    assert ess.loss_mwh == pytest.approx(14.4)
    assert ess.energy_discharged_mwh / ess.energy_charged_mwh == pytest.approx(0.64)


def test_power_balance_includes_ess(two_price_hours, make_ess):
    """수급 균형: Σ p + 방전 - 충전 = 순수요."""
    gens, inp = two_price_hours
    result = solve(inp, ess=make_ess(soc_final_min_pct=50.0))

    for t in range(result.horizon_h):
        assert (
            result.supply_mw(t) + result.ess_net_mw(t)
            == pytest.approx(result.net_demand_mw[t])
        )
    assert validation.check_power_balance(result) == []


# ── 2. 제약이 구속하는지 ─────────────────────────────────────────────────────


def test_charge_power_limit_binds(two_price_hours, make_ess):
    """충전 출력 한계가 10 MW 면 그 이상 충전하지 않는다."""
    _, inp = two_price_hours
    result = solve(inp, ess=make_ess(power_charge_mw=10.0))

    assert result.ess.charge_mw[0] == pytest.approx(10.0)


def test_discharge_power_limit_binds(two_price_hours, make_ess):
    """방전 출력 한계가 5 MW 면 비싼 시간에 5 MW 만 낸다."""
    _, inp = two_price_hours
    result = solve(inp, ess=make_ess(power_discharge_mw=5.0))

    assert result.ess.discharge_mw[1] == pytest.approx(5.0)


def test_soc_max_limits_charging(two_price_hours, make_ess):
    """에너지 용량이 작으면 출력이 아니라 SOC 상한이 충전을 묶는다.

    용량 220 MWh, 초기 SOC 50% = 110, 상한 100% = 220
    -> 충전 가능 에너지 110 MWh, 충전 출력 환산 110 / 0.8 = 137.5 MW 이지만
       출력 한계 100 MW 가 아니라 t=0 의 cheap 잔여 40 MW 가 먼저 묶는다.
    용량을 40 MWh 로 줄이면 SOC 상한이 구속한다: (40 - 20) / 0.8 = 25 MW.
    """
    _, inp = two_price_hours
    result = solve(inp, ess=make_ess(capacity_mwh=40.0))

    assert result.ess.charge_mw[0] == pytest.approx(25.0)
    assert result.ess.soc_mwh[0] == pytest.approx(40.0)
    assert validation.check_ess(result) == []


def test_final_soc_blocks_free_energy(make_generator, make_input, make_ess):
    """종료 SOC 하한이 있으면 배터리를 비우고 끝낼 수 없다.

    비싼 유닛만 있고 1시간뿐이라 충전 기회가 없다. 종료 SOC 하한이 초기와 같으면
    방전이 0 이어야 하고, 하한이 0 이면 방전해서 비용이 내려간다.
    """
    pricey = make_generator(gen_id="pricey", p_max_mw=200.0, p_min_mw=0.0,
                            fuel_cost_krw_per_mwh=100_000)
    inp = make_input([pricey], [100.0], reserve_margin_ratio=0.0)

    locked = solve(inp, ess=make_ess(soc_final_min_pct=50.0))
    free = solve(inp, ess=make_ess(soc_final_min_pct=0.0))

    assert locked.ess.discharge_mw == pytest.approx([0.0])
    assert locked.total_cost_krw == pytest.approx(100 * 100_000)
    # 하한 0 이면 SOC 200 MWh 를 꺼내 쓴다 (출력 한계 100 MW 가 먼저 묶는다)
    assert free.ess.discharge_mw == pytest.approx([100.0])
    assert free.total_cost_krw == pytest.approx(0.0)
    assert free.ess.soc_mwh[0] == pytest.approx(200.0 - 100.0 / 0.8)
    assert validation.check_ess(locked) == []
    assert validation.check_ess(free) == []


def test_no_simultaneous_charge_and_discharge(two_price_hours, make_ess):
    """이진변수를 안 써도 동시 충·방전은 나오지 않는다 (왕복효율 < 1 이라 손실)."""
    _, inp = two_price_hours
    result = solve(inp, ess=make_ess(enforce_exclusive=False))

    for t in range(result.horizon_h):
        assert min(result.ess.charge_mw[t], result.ess.discharge_mw[t]) == pytest.approx(0.0)


def test_enforce_exclusive_adds_binaries(two_price_hours, make_ess):
    """금지를 명시하면 시간당 이진변수가 1개 늘고, 해는 달라지지 않는다."""
    _, inp = two_price_hours
    relaxed = solve(inp, ess=make_ess(soc_final_min_pct=50.0))
    strict = solve(inp, ess=make_ess(soc_final_min_pct=50.0, enforce_exclusive=True))

    assert strict.status == "optimal"
    assert strict.size.n_binaries == relaxed.size.n_binaries + inp.horizon_h
    assert strict.total_cost_krw == pytest.approx(relaxed.total_cost_krw)
    assert validation.check_ess(strict) == []


def test_reserve_uses_generator_load_with_ess(make_generator, make_input, make_ess):
    """예비율 분모는 발전기 부하(수요 + 충전 - 방전)다."""
    gens = [make_generator(gen_id=f"g{i}", p_max_mw=100.0, p_min_mw=0.0) for i in range(3)]
    inp = make_input(gens, [150.0, 150.0], reserve_margin_ratio=0.10)
    result = solve(inp, ess=make_ess())

    for t in range(result.horizon_h):
        load = result.generator_load_mw(t)
        assert result.available_capacity_mw(t) >= 1.1 * load - 1e-6
    assert validation.check_reserve(result) == []


# ── 3. 피크 감소 ─────────────────────────────────────────────────────────────


@pytest.fixture
def peaky_day(make_generator, make_input):
    """낮에 뾰족한 피크가 있는 6시간 문제.

    램프 한계는 피크 검증과 무관하므로 p_max 만큼 열어 둔다 (ESS 효과만 본다).
    """
    base = make_generator(gen_id="base", p_max_mw=1000.0, p_min_mw=0.0,
                          ramp_up_mw_per_h=1000.0, ramp_down_mw_per_h=1000.0,
                          fuel_cost_krw_per_mwh=10_000)
    demand = [400.0, 380.0, 500.0, 900.0, 600.0, 420.0]
    return [base], make_input([base], demand, reserve_margin_ratio=0.0)


def test_peak_reduction_definition(peaky_day, make_ess):
    """피크 감소율 = (ESS 미적용 순수요 피크 - 적용 후 발전기 부하 피크) / 전 x 100."""
    _, inp = peaky_day
    ess = make_ess(power_charge_mw=50.0, power_discharge_mw=50.0, capacity_mwh=400.0,
                   mode="peak_shaving", peak_target_pct=5.0, soc_final_min_pct=0.0)
    result = solve(inp, ess=ess)

    assert result.status == "optimal"
    assert result.peak_demand_mw == pytest.approx(900.0)
    assert result.peak_after_ess_mw <= 900.0 * 0.95 + 1e-6
    expected = (900.0 - result.peak_after_ess_mw) / 900.0 * 100.0
    assert result.peak_reduction_pct == pytest.approx(expected)
    assert result.peak_reduction_pct >= 5.0


def test_peak_shaving_target_is_hard_constraint(peaky_day, make_ess):
    """목표를 달성할 수 없으면 infeasible 로 드러난다 (성공을 만들지 않는다)."""
    _, inp = peaky_day
    ess = make_ess(power_discharge_mw=10.0, mode="peak_shaving", peak_target_pct=50.0)

    assert solve(inp, ess=ess).status == "infeasible"


def test_cost_mode_does_not_guarantee_peak_reduction(make_generator, make_input, make_ess):
    """단가가 시간에 따라 다르지 않으면 비용 최소화는 ESS 를 쓸 이유가 없다.

    이 경우 피크 감소율은 0 이다. 그래서 5% KPI 는 peak_shaving 모드로 측정한다.
    """
    flat = make_generator(gen_id="flat", p_max_mw=1000.0, p_min_mw=0.0,
                          ramp_up_mw_per_h=1000.0, ramp_down_mw_per_h=1000.0,
                          fuel_cost_krw_per_mwh=10_000)
    inp = make_input([flat], [400.0, 900.0, 500.0], reserve_margin_ratio=0.0)
    result = solve(inp, ess=make_ess(mode="cost", soc_final_min_pct=50.0))

    assert result.status == "optimal"
    assert result.peak_reduction_pct == pytest.approx(0.0)
    assert sum(result.ess.discharge_mw) == pytest.approx(0.0)


def test_peak_shaving_charges_before_the_peak(peaky_day, make_ess):
    """피크를 깎으려면 그 전에 충전해 둬야 한다 (SOC 가 피크 전에 올라간다)."""
    _, inp = peaky_day
    ess = make_ess(power_charge_mw=50.0, power_discharge_mw=50.0,
                   mode="peak_shaving", peak_target_pct=5.0, soc_init_pct=0.0,
                   soc_final_min_pct=0.0)
    result = solve(inp, ess=ess)

    assert result.status == "optimal"
    peak_hour = result.net_demand_mw.index(max(result.net_demand_mw))
    assert sum(result.ess.charge_mw[:peak_hour]) > 0
    assert result.ess.discharge_mw[peak_hour] > 0


def test_ess_losses_can_increase_emissions(two_price_hours, make_ess):
    """왕복효율 손실만큼 추가 발전이 필요하므로 배출은 늘 수 있다.

    비용은 내려가고 탄소는 올라가는 trade-off 를 숫자로 남긴다 (탄소 모듈과 연결).
    """
    _, inp = two_price_hours
    without = solve(inp)
    with_ess = solve(inp, ess=make_ess(soc_final_min_pct=50.0))

    assert with_ess.total_cost_krw < without.total_cost_krw
    diff = carbon.compare(without, with_ess)
    assert diff["after_ton"] >= diff["before_ton"]


# ── 4. 검사기 ────────────────────────────────────────────────────────────────


def bad_result(inp, ess_spec, charge, discharge, soc, generators):
    """손으로 만든 (틀린) ESS 스케줄을 결과에 끼워 넣는다."""
    horizon = inp.horizon_h
    return assemble(
        "manual", "feasible", inp,
        {g.id: [True] * horizon for g in generators},
        {g.id: [inp.net_demand_mw[t] for t in range(horizon)] for g in generators},
        effective_p_max(generators, 1.0),
        [0.0] * horizon,
        shed_penalty_krw_per_mwh=0.0,
        ess=EssSchedule(charge_mw=charge, discharge_mw=discharge, soc_mwh=soc,
                        spec=ess_spec),
    )


def test_check_ess_catches_power_violation(make_generator, make_input, make_ess):
    gen = make_generator(gen_id="g1", p_min_mw=0.0)
    inp = make_input([gen], [50.0])
    spec = make_ess(power_charge_mw=10.0)
    result = bad_result(inp, spec, [50.0], [0.0], [200 + 0.8 * 50], [gen])

    kinds = {v.constraint for v in validation.check_ess(result)}
    assert "ess_power" in kinds


def test_check_ess_catches_soc_bounds(make_generator, make_input, make_ess):
    gen = make_generator(gen_id="g1", p_min_mw=0.0)
    inp = make_input([gen], [50.0])
    spec = make_ess(capacity_mwh=400.0, soc_max_pct=100.0)
    result = bad_result(inp, spec, [0.0], [0.0], [500.0], [gen])

    kinds = {v.constraint for v in validation.check_ess(result)}
    assert "ess_soc_bounds" in kinds


def test_check_ess_catches_transition_error(make_generator, make_input, make_ess):
    """효율을 빼먹은 SOC(충전량을 그대로 더한 값)를 잡는다."""
    gen = make_generator(gen_id="g1", p_min_mw=0.0)
    inp = make_input([gen], [50.0])
    spec = make_ess()
    result = bad_result(inp, spec, [50.0], [0.0], [250.0], [gen])  # 올바른 값은 240

    violations = validation.check_ess(result)
    assert [v.constraint for v in violations] == ["ess_soc_balance"]
    assert "전이식" in violations[0].detail


def test_check_ess_catches_final_soc(make_generator, make_input, make_ess):
    gen = make_generator(gen_id="g1", p_min_mw=0.0)
    inp = make_input([gen], [50.0])
    spec = make_ess(soc_final_min_pct=50.0)
    result = bad_result(inp, spec, [0.0], [80.0], [200.0 - 100.0], [gen])

    kinds = {v.constraint for v in validation.check_ess(result)}
    assert "ess_soc_final" in kinds


def test_check_ess_catches_simultaneous_when_enforced(make_generator, make_input, make_ess):
    gen = make_generator(gen_id="g1", p_min_mw=0.0)
    inp = make_input([gen], [50.0])
    spec = make_ess(enforce_exclusive=True)
    soc = [200.0 + 0.8 * 20.0 - 20.0 / 0.8]
    result = bad_result(inp, spec, [20.0], [20.0], soc, [gen])

    kinds = {v.constraint for v in validation.check_ess(result)}
    assert "ess_exclusive" in kinds


def test_report_includes_ess_checks_only_when_ess_used(two_price_hours, make_ess):
    """ESS 를 안 쓴 해에 ESS 항목을 '통과'로 보여주지 않는다."""
    gens, inp = two_price_hours

    without = validation.report(solve(inp), gens)
    with_ess = validation.report(solve(inp, ess=make_ess(soc_final_min_pct=50.0)), gens)

    assert not any(c.name.startswith("ESS") for c in without)
    ess_checks = [c for c in with_ess if c.name.startswith("ESS")]
    assert len(ess_checks) == 5
    assert all(c.ok for c in ess_checks)


# ── 5. combined 모드 ─────────────────────────────────────────────────────────


def test_combined_mode_trades_cost_against_peak(peaky_day, make_ess):
    """비용 + lambda x 피크 가중합. lambda 가 크면 피크를 더 깎는다.

    단가가 시간에 따라 같으면 lambda = 0 에서는 ESS 를 쓸 이유가 없다 (손실만 난다).
    lambda 를 키우면 피크를 깎는 대신 왕복효율 손실만큼 비용이 올라간다.
    종료 SOC 하한을 초기와 같게 둬서 "초기 SOC 를 꺼내 쓰는" 공짜 에너지를 배제한다.
    """
    _, inp = peaky_day
    spec = dict(power_charge_mw=50.0, power_discharge_mw=50.0, mode="combined",
                soc_final_min_pct=50.0)

    neutral = solve(inp, ess=make_ess(peak_penalty_krw_per_mw=0.0, **spec))
    weighted = solve(inp, ess=make_ess(peak_penalty_krw_per_mw=1_000_000.0, **spec))

    assert neutral.status == weighted.status == "optimal"
    assert neutral.peak_after_ess_mw == pytest.approx(900.0)   # 가중치 0 -> ESS 미사용
    assert weighted.peak_after_ess_mw == pytest.approx(850.0)  # 방전 한계 50 MW 만큼
    assert weighted.operating_cost_krw > neutral.operating_cost_krw  # 손실만큼 더 든다
    assert any("combined" in note for note in weighted.notes)
    assert validation.all_violations(weighted, inp.generators) == []


def test_combined_mode_cost_is_still_pure_accounting(peaky_day, make_ess):
    """목적함수에 피크 가중치가 섞여도 보고되는 비용은 연료비 + 기동비뿐이다."""
    _, inp = peaky_day
    result = solve(inp, ess=make_ess(mode="combined", peak_penalty_krw_per_mw=5_000.0,
                                     soc_final_min_pct=0.0))

    assert result.total_cost_krw == pytest.approx(
        result.fuel_cost_krw + result.startup_cost_krw + result.shed_cost_krw
    )


def test_check_ess_catches_negative_power(make_generator, make_input, make_ess):
    """음수 충·방전은 물리적으로 불가능하다."""
    gen = make_generator(gen_id="g1", p_min_mw=0.0)
    inp = make_input([gen], [50.0])
    spec = make_ess()
    result = bad_result(inp, spec, [-10.0], [0.0], [200.0 - 8.0], [gen])

    kinds = {v.constraint for v in validation.check_ess(result)}
    assert "ess_power" in kinds
