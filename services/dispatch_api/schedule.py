"""급전 계획 조립과 비용 회계.

MILP(`milp.py`)와 Rule-based baseline(`baseline.py`)이 **같은 회계**를 쓰도록
계산을 여기 한 곳에만 둔다. 절감률 KPI 는 두 방식의 total_cost_krw 비교이므로,
회계가 갈라지면 숫자가 의미를 잃는다 (CLAUDE.local.md: 같은 cost accounting).

전부 순수 함수다. solver 도 config 파일도 모른다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from models import (
    DispatchInput,
    DispatchResult,
    EssSchedule,
    Generator,
    ModelSize,
    UnitSchedule,
)

# 급전 결과에서 "출력 0" 과 수치오차를 구분하는 기준. solver 는 1e-9 수준의
# 쓰레기값을 남기므로 그대로 리포트하면 정지한 유닛이 켜진 것처럼 보인다.
ZERO_MW = 1e-6


def effective_p_max(generators: Sequence[Generator], hydro_factor: float) -> dict[str, float]:
    """저수율 제한을 반영한 유닛별 출력 상한 (MW).

    수력만 영향을 받는다. hydro_factor 는 `HydroPolicy.factor()` 가 만든다.
    """
    return {
        g.id: g.p_max_mw * (hydro_factor if g.fuel == "hydro" else 1.0)
        for g in generators
    }


def startup_ramp_mw(g: Generator, p_max_eff: float) -> float:
    """기동한 시간에 올릴 수 있는 출력 (MW).

    [모델링 선택] 기동 시점의 램프 한계를 max(p_min, ramp_up) 로 둔다.
      - p_min 을 포함하는 이유: 유닛은 켜지는 순간 최소출력 이상이어야 한다
        (`p >= p_min * u`). ramp_up 만 쓰면 ramp_up < p_min 인 유닛은 영원히
        켜질 수 없어, 제약이 아니라 버그처럼 동작한다.
      - ramp_up 을 포함하는 이유: p_min 이 0 인 유닛(수력)이 기동한 시간에
        0 밖에 못 내는 상황을 막는다.
    상한은 p_max_eff 다.
    """
    return min(max(g.p_min_mw, g.ramp_up_mw_per_h), p_max_eff)


def shutdown_ramp_mw(g: Generator, p_max_eff: float) -> float:
    """정지하는 시간에 내릴 수 있는 출력 (MW). 기동 쪽과 같은 이유로 대칭이다."""
    return min(max(g.p_min_mw, g.ramp_down_mw_per_h), p_max_eff)


def startup_flags(on: Sequence[bool], u_init: int) -> list[bool]:
    """t 에 기동했는지. 초기 상태(u_init)를 t=-1 로 본다."""
    prev = bool(u_init)
    flags = []
    for state in on:
        flags.append(state and not prev)
        prev = state
    return flags


def shutdown_flags(on: Sequence[bool], u_init: int) -> list[bool]:
    """t 에 정지했는지."""
    prev = bool(u_init)
    flags = []
    for state in on:
        flags.append(prev and not state)
        prev = state
    return flags


def assemble(
    method: str,
    status: str,
    inp: DispatchInput,
    on: Mapping[str, Sequence[bool]],
    p_mw: Mapping[str, Sequence[float]],
    p_max_eff: Mapping[str, float],
    shed_mw: Sequence[float],
    *,
    shed_penalty_krw_per_mwh: float,
    surplus_mw: Sequence[float] | None = None,
    hydro_factor: float = 1.0,
    ess: EssSchedule | None = None,
    size: ModelSize | None = None,
    notes: Sequence[str] | None = None,
) -> DispatchResult:
    """해(解)를 DispatchResult 로 묶고 비용을 집계한다.

    비용은 solver 의 목적함수 값을 쓰지 않고 **해에서 다시 계산**한다. baseline 은
    목적함수가 없으므로, 같은 식으로 계산해야 비교가 성립한다. 부수적으로
    목적함수 값과 비교하면 모델링 실수(계수 누락)를 잡는 교차검증이 된다.

    time step 이 1시간이므로 출력(MW) 합이 곧 에너지(MWh)다.
    """
    units: list[UnitSchedule] = []
    fuel_cost = 0.0
    startup_cost = 0.0

    for g in inp.generators:
        states = [bool(x) for x in on[g.id]]
        outputs = [0.0 if abs(x) < ZERO_MW else float(x) for x in p_mw[g.id]]
        starts = startup_flags(states, g.u_init)

        fuel_cost += g.fuel_cost_krw_per_mwh * sum(outputs)
        startup_cost += g.startup_cost_krw * sum(starts)

        units.append(UnitSchedule(
            unit_id=g.id,
            fuel=g.fuel,
            on=states,
            p_mw=outputs,
            startup=starts,
            shutdown=shutdown_flags(states, g.u_init),
            p_max_eff_mw=float(p_max_eff[g.id]),
            fuel_cost_krw_per_mwh=g.fuel_cost_krw_per_mwh,
            startup_cost_krw=g.startup_cost_krw,
            co2_ton_per_mwh=g.co2_ton_per_mwh,
        ))

    shed = [0.0 if abs(x) < ZERO_MW else float(x) for x in shed_mw]

    return DispatchResult(
        method=method,
        status=status,
        timestamps=list(inp.timestamps),
        net_demand_mw=list(inp.net_demand_mw),
        units=units,
        shed_mw=shed,
        fuel_cost_krw=fuel_cost,
        startup_cost_krw=startup_cost,
        shed_cost_krw=shed_penalty_krw_per_mwh * sum(shed),
        reserve_margin_ratio=inp.reserve_margin_ratio,
        hydro_factor=hydro_factor,
        ess=ess,
        size=size,
        notes=list(notes or []),
        surplus_mw=[float(x) for x in (surplus_mw or [0.0] * len(shed))],
    )


def cost_reduction_pct(baseline: DispatchResult, optimized: DispatchResult) -> float:
    """(baseline - optimized) / baseline x 100. KPI 기준 >= 10%.

    CLAUDE.local.md 의 정의식 그대로다. 둘 다 total_cost_krw(연료비+기동비+
    페널티)를 쓰므로, 한쪽만 부하차단을 쓰면 그 페널티가 비교에 그대로 들어간다.
    """
    base = baseline.total_cost_krw
    if base <= 0:
        return 0.0
    return (base - optimized.total_cost_krw) / base * 100.0


def merit_order(result: DispatchResult) -> list[dict[str, object]]:
    """연료비 오름차순 급전 순위와 실제 투입량. Merit Order Stack 시각화용.

    같은 연료의 유닛을 묶어 (연료비, 투입 에너지, 가동 시간, 설비 상한)을 돌려준다.
    """
    by_fuel: dict[str, dict[str, float]] = {}
    for unit in result.units:
        row = by_fuel.setdefault(unit.fuel, {
            "fuel_cost_krw_per_mwh": unit.fuel_cost_krw_per_mwh,
            "capacity_mw": 0.0,
            "energy_mwh": 0.0,
            "unit_hours_on": 0.0,
            "n_units": 0.0,
            "co2_ton": 0.0,
        })
        row["capacity_mw"] += unit.p_max_eff_mw
        row["energy_mwh"] += unit.energy_mwh
        row["unit_hours_on"] += sum(unit.on)
        row["n_units"] += 1
        row["co2_ton"] += unit.co2_ton_per_mwh * unit.energy_mwh

    ordered = sorted(by_fuel.items(), key=lambda kv: kv[1]["fuel_cost_krw_per_mwh"])
    return [{"rank": i + 1, "fuel": fuel, **row} for i, (fuel, row) in enumerate(ordered)]
