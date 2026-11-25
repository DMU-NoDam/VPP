"""동일 입력을 고정한 ε-constraint 비용·탄소 표본 분석."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

import carbon
import milp
from models import DispatchInput, DispatchResult, EssSpec, HydroPolicy


@dataclass
class Attempt:
    label: str
    cap_ton: float | None
    result: DispatchResult
    elapsed_s: float


@dataclass
class Sweep:
    attempts: list[Attempt] = field(default_factory=list)
    reference: Attempt | None = None
    minimum: Attempt | None = None
    minimum_emissions_ton: float | None = None
    complete: bool = False
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)


def run(
    inp: DispatchInput,
    *,
    solver_config: milp.SolverConfig,
    hydro_policy: HydroPolicy,
    ess: EssSpec | None,
    points: int,
    total_time_limit_s: float,
    emission_tolerance_ton: float,
    reduction_targets_pct: list[float],
) -> Sweep:
    """총 시간 예산 내 순차 풀이. gap=0, 부하차단 금지, 입력 재로딩 없음.

    상한 표본은 전체 Pareto 집합이 아니다. 타임아웃/무해 결과도 attempts에 보존한다.
    시간 예산은 다음 풀이 시작 및 solver 제한에 적용하며 모델 빌드·직렬화는 소폭 초과 가능.
    """
    started = time.perf_counter()
    sweep = Sweep()

    def solve(label: str, cap: float | None = None, mode: str = "cost") -> Attempt | None:
        remaining = total_time_limit_s - (time.perf_counter() - started)
        if remaining <= 0:
            sweep.notes.append("전체 시간 예산 소진; 나머지 점은 계산하지 않음")
            return None
        limit = min(remaining, solver_config.time_limit_s or remaining)
        config = replace(solver_config, time_limit_s=limit, mip_gap=0.0)
        begin = time.perf_counter()
        result = milp.solve(
            inp, solver_config=config, hydro_policy=hydro_policy, ess=ess,
            carbon_cap_ton=cap, objective_mode=mode,
        )
        attempt = Attempt(label, cap, result, time.perf_counter() - begin)
        sweep.attempts.append(attempt)
        return attempt

    def finish() -> Sweep:
        sweep.elapsed_s = time.perf_counter() - started
        return sweep

    sweep.reference = solve("cost_reference")
    if sweep.reference is None or sweep.reference.result.status != "optimal":
        sweep.notes.append("비용 최소 기준 해의 최적성 미확인; 감축 범위 계산 중단")
        return finish()
    lower = solve("minimum_carbon", mode="carbon")
    if lower is None or lower.result.status != "optimal":
        sweep.notes.append("최소 배출량의 최적성 미확인; 최대 감축률은 제공하지 않음")
        return finish()
    e_min = carbon.total_ton(lower.result.units)
    e_ref = carbon.total_ton(sweep.reference.result.units)
    sweep.minimum_emissions_ton = e_min
    sweep.minimum = solve("minimum_carbon_cost_tiebreak", e_min + emission_tolerance_ton)
    if sweep.minimum is None or sweep.minimum.result.status != "optimal":
        sweep.notes.append("최소 배출 해의 비용 동점 처리 미완료")
        return finish()

    caps = [
        (f"frontier_{i}", e_ref - (e_ref - e_min) * i / (points - 1))
        for i in range(1, points - 1)
    ] if e_ref - e_min > emission_tolerance_ton else []
    caps += [(f"target_{target:g}pct", e_ref * (1 - target / 100))
             for target in reduction_targets_pct]
    completed = True
    for label, cap in caps:
        attempt = solve(label, max(0.0, cap))
        if attempt is None:
            completed = False
            break
        if attempt.result.status not in ("optimal", "infeasible"):
            completed = False
    sweep.complete = completed
    return finish()


@dataclass
class Reduction:
    """비용 최소 계획을 기준으로 목표만큼 배출을 줄인 계획 하나의 계산 결과."""

    target_pct: float
    cap_ton: float
    reference_ton: float
    plan: DispatchResult | None = None      # 상한을 걸고 푼 계획 (solve 를 건너뛰면 None)
    plan_ton: float | None = None
    minimum: DispatchResult | None = None   # 목표가 불가능할 때만 계산한다
    minimum_ton: float | None = None
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)


def reduce_to_target(
    inp: DispatchInput,
    reference: DispatchResult,
    *,
    solver_config: milp.SolverConfig,
    hydro_policy: HydroPolicy,
    ess: EssSpec | None,
    target_pct: float,
    emission_tolerance_ton: float,
    time_limit_s: float,
) -> Reduction:
    """이미 푼 비용 최소 해를 기준으로 ε-constraint 한 점만 계산한다.

    기준 해를 다시 풀지 않으므로 solve 는 1회만 늘어난다 (목표가 불가능하면 최대
    감축 가능량 확인에 1회 더). 두 solve 를 합쳐 `time_limit_s` 안에서 끝낸다.
    기본 급전 계획을 대체하지 않는 부가 분석이다 (docs/formulation.md 6.2).
    """
    started = time.perf_counter()
    ref_ton = carbon.total_ton(reference.units)
    red = Reduction(
        target_pct=target_pct,
        cap_ton=max(0.0, ref_ton * (1.0 - target_pct / 100.0)),
        reference_ton=ref_ton,
    )
    if ref_ton <= emission_tolerance_ton:
        red.notes.append("기준 계획의 배출량이 0에 가까워 감축할 여지가 없다")
        red.elapsed_s = time.perf_counter() - started
        return red

    def solve(**kwargs: object) -> DispatchResult | None:
        remaining = time_limit_s - (time.perf_counter() - started)
        if remaining <= 0:
            red.notes.append("감축 분석 시간 예산 소진; 계산하지 않음")
            return None
        limit = min(remaining, solver_config.time_limit_s or remaining)
        config = replace(solver_config, time_limit_s=limit, mip_gap=0.0)
        return milp.solve(inp, solver_config=config, hydro_policy=hydro_policy, ess=ess, **kwargs)

    red.plan = solve(carbon_cap_ton=red.cap_ton)
    if red.plan is not None and red.plan.has_solution:
        red.plan_ton = carbon.total_ton(red.plan.units)
    elif red.plan is not None:
        # 목표를 못 맞췄으면 "얼마까지 줄일 수 있는지"를 배출 최소 목적으로 확인한다.
        if ess is not None and ess.mode == "combined":
            red.notes.append("ESS combined 모드에서는 최대 감축 가능량을 계산하지 않는다")
        else:
            red.minimum = solve(objective_mode="carbon")
            if red.minimum is not None and red.minimum.has_solution:
                red.minimum_ton = carbon.total_ton(red.minimum.units)
            elif red.minimum is not None:
                red.notes.append("최대 감축 가능량의 최적성을 확인하지 못했다")
    red.elapsed_s = time.perf_counter() - started
    return red
