"""Rule-based baseline — 우선순위(Merit Order) 급전 (docs/formulation.md 3장).

MILP 의 비교 대상이다. KPI 는 `(C_rule - C_milp) / C_rule >= 10%` 이지만,
baseline 을 일부러 약하게 만들면 비교의 설득력이 사라진다 (CLAUDE.local.md).
그래서 "현장에서 실제로 쓰는 우선순위법(priority list)"을 그대로 구현한다.

MILP 와 같은 것
    같은 입력(DispatchInput), 같은 제약(상하한·램프·최소시간·예비율·수력 제한),
    같은 cost accounting(연료비 + 기동비), 같은 결과 타입(DispatchResult).

MILP 와 다른 것 — 이것이 비용 차이의 원인이다
    R1 우선순위: 연료비 오름차순 (동률이면 큰 유닛 먼저)
    R2 시야: 매 시간 독립 결정. 다음 시간을 보지 않는다 (no lookahead)
    R3 기동: 그 시간의 (1+r)x수요를 채울 때까지 우선순위대로 투입
    R4 정지: 빼도 예비율이 유지되고 최소시간·램프가 허용하면 비싼 유닛부터 뺀다
    R5 급전: 투입된 유닛을 우선순위대로 상한까지 채운다
    R6 ESS 없음, 예측 사용 없음

즉 baseline 은 "지금 이 시간"만 보고 최적이고, MILP 는 24시간 전체를 본다.
차이는 기동비와 Pmin 과잉발전의 시간 간 조정에서 나온다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from models import DispatchInput, DispatchResult, Generator, HydroPolicy
from schedule import assemble, effective_p_max, shutdown_ramp_mw, startup_ramp_mw

logger = logging.getLogger(__name__)


def priority_order(generators: list[Generator]) -> list[Generator]:
    """R1. 연료비 오름차순, 동률이면 큰 유닛 먼저, 그다음 id 순(재현성)."""
    return sorted(generators, key=lambda g: (g.fuel_cost_krw_per_mwh, -g.p_max_mw, g.id))


@dataclass
class _UnitState:
    """유닛의 직전 시간 상태. 최소 기동/정지 시간과 램프의 초기 조건이다.

    `p_mw = None` 은 "직전 출력을 모른다"는 뜻이고, u_init=1 인데 p_init_mw 가
    주어지지 않은 t=0 에서만 생긴다. 이때는 램프 제약을 걸지 않는다 —
    MILP 도 같은 조건에서 t=0 램프 제약을 생략하므로, 한쪽만 묶이면 비교가
    공정하지 않다 (CLAUDE.local.md: 가능한 한 같은 제약 조건 범위에서 비교).
    """

    on: bool
    elapsed_h: int
    p_mw: float | None

    def locked_on(self, g: Generator) -> bool:
        return self.on and self.elapsed_h < g.min_up_h

    def locked_off(self, g: Generator) -> bool:
        return not self.on and self.elapsed_h < g.min_down_h


def _blocked_by_ramp_down(g: Generator, prev: _UnitState, p_max_eff: float) -> bool:
    """지금 세우면 램프다운 제약을 위반하는지. 모르는 직전 출력은 묶지 않는다."""
    if not prev.on or prev.p_mw is None:
        return False
    return prev.p_mw > shutdown_ramp_mw(g, p_max_eff)


def _commit(
    order: list[Generator],
    state: dict[str, _UnitState],
    p_max_eff: dict[str, float],
    demand_mw: float,
    reserve_ratio: float,
) -> dict[str, bool]:
    """R3 + R4. 그 시간의 기동 상태를 정한다."""
    required_capacity = (1 + reserve_ratio) * demand_mw

    # 직전 시간에 돌던 유닛은 일단 계속 돈다. 매 시간 0 에서 다시 정하면
    # "돌고 있는 발전기를 아무 이유 없이 세우는" 운영이 되고, 램프다운 제약도
    # 위반하게 된다. 해제는 아래 R4 가 조건을 보고 한다.
    committed = {g.id: state[g.id].on for g in order}
    capacity = sum(p_max_eff[g.id] for g in order if committed[g.id])

    # R3 투입: 우선순위대로, 예비율 요건을 채울 때까지.
    for g in order:
        if capacity >= required_capacity:
            break
        if committed[g.id] or state[g.id].locked_off(g):
            continue
        committed[g.id] = True
        capacity += p_max_eff[g.id]

    # R4 해제: 비싼 유닛부터, 빼도 되는 조건을 모두 만족하면 뺀다.
    #   (a) 더 싼 유닛들의 용량만으로 그 시간 수요가 충족된다
    #       -> 이 유닛은 어차피 급전되지 않는다. 이 조건을 빼면 "가장 싼 유닛을
    #          세우고 비싼 유닛을 돌리는" 급전이 나와 baseline 이 부당하게 나빠진다
    #   (b) 빼도 예비율 요건이 유지된다
    #   (c) 램프다운이 허용한다 — 출력이 높은 유닛을 바로 세우면 제약 위반이다
    #       ("세우기 전에 최소출력까지 내려야 한다")
    cheaper_capacity = 0.0
    needed_for_energy = {}
    for g in order:
        needed_for_energy[g.id] = cheaper_capacity < demand_mw
        if committed[g.id]:
            cheaper_capacity += p_max_eff[g.id]

    for g in reversed(order):
        if not committed[g.id] or state[g.id].locked_on(g):
            continue
        if needed_for_energy[g.id]:
            continue
        if capacity - p_max_eff[g.id] < required_capacity:
            continue
        if _blocked_by_ramp_down(g, state[g.id], p_max_eff[g.id]):
            continue
        committed[g.id] = False
        capacity -= p_max_eff[g.id]

    # Pmin 과잉발전 해제: 투입된 유닛의 최소출력 합이 수요를 넘으면 수급 균형을
    # 맞출 수 없다. 비싼 유닛부터 더 뺀다 (예비율보다 균형이 우선이다).
    def min_output() -> float:
        return sum(g.p_min_mw for g in order if committed[g.id])

    for g in reversed(order):
        if min_output() <= demand_mw:
            break
        if not committed[g.id] or state[g.id].locked_on(g):
            continue
        if _blocked_by_ramp_down(g, state[g.id], p_max_eff[g.id]):
            continue
        committed[g.id] = False

    return committed


def _dispatch(
    order: list[Generator],
    state: dict[str, _UnitState],
    committed: dict[str, bool],
    p_max_eff: dict[str, float],
    demand_mw: float,
) -> tuple[dict[str, float], float, float]:
    """R5. 투입된 유닛을 우선순위대로 채운다.

    돌려주는 값은 (유닛별 출력, 미충족 수요 MW, 과잉발전 MW)다.
    """
    lo: dict[str, float] = {}
    hi: dict[str, float] = {}
    for g in order:
        if not committed[g.id]:
            lo[g.id] = hi[g.id] = 0.0
            continue
        prev = state[g.id]
        if prev.on and prev.p_mw is None:  # 직전 출력 미지 — 램프 제약 생략
            lo[g.id] = g.p_min_mw
            hi[g.id] = p_max_eff[g.id]
        elif prev.on:
            lo[g.id] = max(g.p_min_mw, prev.p_mw - g.ramp_down_mw_per_h)
            hi[g.id] = min(p_max_eff[g.id], prev.p_mw + g.ramp_up_mw_per_h)
        else:  # 이 시간에 기동 — 기동 램프 한계까지만 올릴 수 있다
            lo[g.id] = g.p_min_mw
            hi[g.id] = min(p_max_eff[g.id], startup_ramp_mw(g, p_max_eff[g.id]))
        lo[g.id] = min(lo[g.id], hi[g.id])

    output = dict(lo)
    residual = demand_mw - sum(output.values())

    if residual < 0:
        # 하한 합이 수요를 넘었다. 하한은 p_min 과 램프다운 한계 중 큰 값이므로,
        # 더 내리려면 물리 제약을 깨야 한다. 규칙에 lookahead 가 없어서 생기는
        # 코너이고(직전 시간에 미리 낮춰 두지 못했다), 과잉분을 숫자로 남긴다.
        return output, 0.0, -residual

    for g in order:  # 싼 유닛부터 상한까지
        if residual <= 0:
            break
        add = min(hi[g.id] - output[g.id], residual)
        output[g.id] += add
        residual -= add

    return output, max(residual, 0.0), 0.0


def run(
    inp: DispatchInput,
    *,
    hydro_policy: HydroPolicy | None = None,
    shed_penalty_krw_per_mwh: float = 0.0,
) -> DispatchResult:
    """우선순위법으로 24시간 급전 계획을 만든다.

    solver 를 쓰지 않으므로 항상 해가 나온다. 대신 규칙으로는 못 맞추는 경우가
    있을 수 있어(용량 부족 등) 미충족 수요를 shed_mw 에 남긴다. 비교 시 페널티를
    0 으로 두면 "규칙으로 못 채운 양"이 비용에 섞이지 않는다.
    """
    policy = hydro_policy or HydroPolicy()
    hydro_factor = policy.factor(inp.storage_rate_pct)
    p_max_eff = effective_p_max(inp.generators, hydro_factor)
    order = priority_order(list(inp.generators))

    state = {
        g.id: _UnitState(
            on=bool(g.u_init),
            elapsed_h=g.init_state_h,
            p_mw=(inp.p_init_mw or {}).get(g.id, None if g.u_init else 0.0),
        )
        for g in inp.generators
    }

    on: dict[str, list[bool]] = {g.id: [] for g in inp.generators}
    p_mw: dict[str, list[float]] = {g.id: [] for g in inp.generators}
    shed_mw: list[float] = []
    surplus_mw: list[float] = []

    for t in range(inp.horizon_h):
        demand = inp.net_demand_mw[t]
        committed = _commit(order, state, p_max_eff, demand, inp.reserve_margin_ratio)
        output, unserved, surplus = _dispatch(order, state, committed, p_max_eff, demand)

        for g in inp.generators:
            on[g.id].append(committed[g.id])
            p_mw[g.id].append(output[g.id])
            prev = state[g.id]
            state[g.id] = _UnitState(
                on=committed[g.id],
                elapsed_h=prev.elapsed_h + 1 if prev.on == committed[g.id] else 1,
                p_mw=output[g.id],
            )
        shed_mw.append(unserved)
        surplus_mw.append(surplus)

        if unserved > 0:
            logger.warning("baseline t=%d: 규칙으로 %.1f MW 를 채우지 못했다", t, unserved)
        if surplus > 0:
            logger.warning(
                "baseline t=%d: 램프 한계로 %.1f MW 를 감발하지 못했다 (과잉발전)", t, surplus
            )

    notes = ["Rule-based: 우선순위 급전 + 고정 예비율, lookahead 없음, ESS 없음"]
    if hydro_factor < 1.0:
        notes.append(f"수력 상한에 factor {hydro_factor:.3f} 적용")
    if sum(shed_mw) > 0:
        notes.append(f"규칙으로 채우지 못한 수요 {sum(shed_mw):,.1f} MWh")
    if sum(surplus_mw) > 0:
        notes.append(
            f"램프 한계로 감발하지 못한 과잉발전 {sum(surplus_mw):,.1f} MWh "
            "(lookahead 가 없는 규칙의 한계)"
        )

    return assemble(
        "rule_based", "feasible", inp, on, p_mw, p_max_eff, shed_mw,
        shed_penalty_krw_per_mwh=shed_penalty_krw_per_mwh,
        surplus_mw=surplus_mw,
        hydro_factor=hydro_factor,
        notes=notes,
    )
