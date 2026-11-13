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
