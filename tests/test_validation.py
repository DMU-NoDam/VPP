"""제약 검사기 자체의 테스트.

검사기는 MILP·baseline 결과를 믿지 않기 위한 장치다. 그러니 검사기가 **위반을
실제로 잡는지**를 따로 확인해야 한다 (잡지 못하는 검사기는 통과를 보장하지 않는다).
그래서 여기서는 일부러 틀린 스케줄을 손으로 만들어 넣는다.
"""

from __future__ import annotations

import validation
from schedule import assemble, effective_p_max


def result_from(inp, on, p_mw, *, shed=None, hydro_factor=1.0, surplus=None):
    """손으로 만든 스케줄을 DispatchResult 로 포장한다."""
    horizon = inp.horizon_h
    return assemble(
        "manual", "feasible", inp, on, p_mw,
        effective_p_max(inp.generators, hydro_factor),
        shed or [0.0] * horizon,
        shed_penalty_krw_per_mwh=0.0,
        surplus_mw=surplus,
        hydro_factor=hydro_factor,
    )


def test_balance_violation_is_caught(make_generator, make_input):
    gen = make_generator(gen_id="g1")
    inp = make_input([gen], [50.0])
    result = result_from(inp, {"g1": [True]}, {"g1": [40.0]})  # 10 MW 모자람

    violations = validation.check_power_balance(result)
    assert len(violations) == 1
    assert "공급-수요" in violations[0].detail


def test_surplus_is_excluded_from_balance(make_generator, make_input):
    """과잉발전으로 기록된 양은 균형식 위반이 아니다."""
    gen = make_generator(gen_id="g1")
    inp = make_input([gen], [50.0])
    result = result_from(inp, {"g1": [True]}, {"g1": [60.0]}, surplus=[10.0])

    assert validation.check_power_balance(result) == []


def test_capacity_violations_are_caught(make_generator, make_input):
    gen = make_generator(gen_id="g1", p_max_mw=100.0, p_min_mw=20.0)
    inp = make_input([gen], [0.0, 0.0, 0.0])
    result = result_from(
        inp,
        {"g1": [True, True, False]},
        {"g1": [120.0, 5.0, 30.0]},  # 상한 초과 / 하한 미달 / 정지 중 발전
    )

    details = [v.detail for v in validation.check_capacity(result, [gen])]
    assert len(details) == 3
    assert any("p_max" in d for d in details)
    assert any("p_min" in d for d in details)
    assert any("정지 상태" in d for d in details)


def test_reserve_violation_is_caught(make_generator, make_input):
    gen = make_generator(gen_id="g1", p_max_mw=100.0)
    inp = make_input([gen], [95.0], reserve_margin_ratio=0.10)  # 필요 104.5 > 100
    result = result_from(inp, {"g1": [True]}, {"g1": [95.0]})

    violations = validation.check_reserve(result)
    assert len(violations) == 1
    assert "예비율" in violations[0].detail


def test_ramp_violation_is_caught(make_generator, make_input):
    gen = make_generator(gen_id="g1", ramp_up_mw_per_h=10.0, ramp_down_mw_per_h=10.0,
                         p_min_mw=0.0)
    inp = make_input([gen], [0.0, 0.0, 0.0])
    result = result_from(inp, {"g1": [True] * 3}, {"g1": [20.0, 60.0, 20.0]})

    kinds = {v.constraint for v in validation.check_ramp(result, [gen])}
    assert kinds == {"ramp_up", "ramp_down"}


def test_ramp_at_t0_needs_p_init(make_generator, make_input):
    """초기 출력을 모르면 t=0 은 검사하지 않고, 주면 검사한다."""
    gen = make_generator(gen_id="g1", ramp_up_mw_per_h=10.0, p_min_mw=0.0)
    inp = make_input([gen], [80.0])
    result = result_from(inp, {"g1": [True]}, {"g1": [80.0]})

    assert validation.check_ramp(result, [gen], None) == []
    violations = validation.check_ramp(result, [gen], {"g1": 20.0})
    assert [v.constraint for v in violations] == ["ramp_up"]


def test_min_up_violation_is_caught(make_generator, make_input):
    """켜자마자 끄면 최소 기동시간 위반이다."""
    gen = make_generator(gen_id="g1", min_up_h=3, u_init=0, init_state_h=10,
                         p_min_mw=0.0)
    inp = make_input([gen], [0.0] * 4)
    result = result_from(inp, {"g1": [True, False, False, False]}, {"g1": [10, 0, 0, 0]})

    violations = validation.check_min_up_down(result, [gen])
    assert [v.constraint for v in violations] == ["min_up"]
    assert "1h < 최소 3h" in violations[0].detail


def test_min_down_violation_is_caught(make_generator, make_input):
    gen = make_generator(gen_id="g1", min_down_h=3, u_init=1, init_state_h=10,
                         p_min_mw=0.0)
    inp = make_input([gen], [0.0] * 4)
    result = result_from(inp, {"g1": [False, True, True, True]}, {"g1": [0, 10, 10, 10]})

    violations = validation.check_min_up_down(result, [gen])
    assert [v.constraint for v in violations] == ["min_down"]


def test_initial_state_counts_toward_min_up(make_generator, make_input):
    """초기 상태가 이어진 시간을 포함해서 센다.

    min_up 3h 인 유닛이 이미 2h 돌았다면, 1h 더 돌고 끄는 것은 위반이 아니다.
    """
    gen = make_generator(gen_id="g1", min_up_h=3, u_init=1, init_state_h=2, p_min_mw=0.0)
    inp = make_input([gen], [0.0] * 3)
    result = result_from(inp, {"g1": [True, False, False]}, {"g1": [10, 0, 0]})

    assert validation.check_min_up_down(result, [gen]) == []


def test_truncated_last_run_is_not_a_violation(make_generator, make_input):
    """horizon 끝에서 잘린 구간은 최소시간 위반으로 보지 않는다."""
    gen = make_generator(gen_id="g1", min_up_h=6, u_init=0, init_state_h=10, p_min_mw=0.0)
    inp = make_input([gen], [0.0] * 3)
    result = result_from(inp, {"g1": [False, False, True]}, {"g1": [0, 0, 10]})

    assert validation.check_min_up_down(result, [gen]) == []


def test_hydro_limit_mismatch_is_caught(make_generator, make_input):
    """저수율 계수가 상한에 반영되지 않았으면 잡는다."""
    hydro = make_generator(gen_id="hydro_1", fuel="hydro", p_max_mw=100.0, p_min_mw=0.0,
                           fuel_cost_krw_per_mwh=0.0)
    inp = make_input([hydro], [50.0], storage_rate_pct=15.0)
    # factor 는 0.75 라고 보고하면서 상한은 100 그대로인 (잘못된) 결과
    bad = assemble(
        "manual", "feasible", inp, {"hydro_1": [True]}, {"hydro_1": [50.0]},
        {"hydro_1": 100.0}, [0.0], shed_penalty_krw_per_mwh=0.0, hydro_factor=0.75,
    )

    violations = validation.check_hydro_limit(bad, [hydro])
    assert [v.constraint for v in violations] == ["hydro_limit"]


def test_report_lists_every_required_constraint(make_generator, make_input):
    """리포트는 필수 제약 체크리스트를 빠짐없이 보여준다."""
    gen = make_generator(gen_id="g1")
    inp = make_input([gen], [50.0])
    result = result_from(inp, {"g1": [True]}, {"g1": [50.0]})

    names = [c.name for c in validation.report(result, [gen])]
    assert names == [
        "수급 균형", "출력 상·하한", "예비율", "램프업", "램프다운",
        "최소 기동시간", "최소 정지시간", "수력 저수율 제한",
        "과잉발전 없음", "부하차단 미사용",
    ]


def test_report_marks_failure_with_detail(make_generator, make_input):
    gen = make_generator(gen_id="g1")
    inp = make_input([gen], [50.0])
    result = result_from(inp, {"g1": [True]}, {"g1": [10.0]})

    failed = {c.name: c.detail for c in validation.report(result, [gen]) if not c.ok}
    assert "수급 균형" in failed
    assert "1건" in failed["수급 균형"]


def test_shed_is_reported_as_check(make_generator, make_input):
    gen = make_generator(gen_id="g1", p_min_mw=0.0)
    inp = make_input([gen], [60.0])
    result = result_from(inp, {"g1": [True]}, {"g1": [50.0]}, shed=[10.0])

    checks = {c.name: c for c in validation.report(result, [gen])}
    assert checks["수급 균형"].ok
    assert not checks["부하차단 미사용"].ok
    assert "10.0 MWh" in checks["부하차단 미사용"].detail


def test_violation_str_includes_location(make_generator, make_input):
    v = validation.Violation("ramp_up", "증가 50 MW > 한계 40 MW", unit_id="coal_1", hour=6)
    assert "coal_1" in str(v)
    assert "t=6" in str(v)
