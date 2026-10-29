"""Unit Commitment + Economic Dispatch MILP (docs/formulation.md 4장).

구성 순서는 집합/인덱스 -> 파라미터 -> 변수 -> 목적함수 -> 제약 -> solver -> 출력이다
(CLAUDE.local.md 모델링 원칙).

집합
    G  발전 유닛 (config/generators.yaml, 급전 가능 발전원만)
    T  시간 0..H-1, step 1시간

변수
    u[g,t] in {0,1}   가동 여부
    v[g,t] in [0,1]   기동 이벤트
    w[g,t] in [0,1]   정지 이벤트
    p[g,t] >= 0       출력 (MW). step 이 1시간이므로 MWh 와 수치가 같다
    s[t]   >= 0       부하차단 슬랙 (옵션, 기본 off)
    ch[t], dis[t] >= 0  ESS 충전·방전 출력 (MW)
    soc[t]            ESS 저장 에너지 (MWh)
    z[t] in {0,1}     동시 충·방전 금지 (옵션)
    peak              ESS 적용 후 최대 발전기 부하 (MW, peak 모드에서만)

v, w 를 이진변수로 두지 않는 이유: u 가 이진이고 연결식 u_t - u_{t-1} = v_t - w_t 가
있으므로 기동비(>= 0)를 최소화하는 해에서 v 는 max(0, u_t - u_{t-1}) 로 정확히
결정된다. 이진변수 수를 늘리지 않아 10초 KPI 에 유리하다. 다만 기동비가 0 인
유닛(수력)에서는 v = w = 0.5 같은 무차별 해가 가능하므로, **보고용 기동/정지
플래그는 v, w 가 아니라 u 에서 다시 계산한다** (`schedule.startup_flags`).
비용은 v 가 실제 기동 횟수보다 작아질 수 없어 영향이 없다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import pulp

from models import (
    DispatchInput,
    DispatchResult,
    EssSchedule,
    EssSpec,
    Generator,
    HydroPolicy,
    ModelSize,
)
from schedule import assemble, effective_p_max, shutdown_ramp_mw, startup_ramp_mw

logger = logging.getLogger(__name__)

# 부하차단 페널티 [모델링 선택]. 과제 필수가 아니라 feasibility 처리용이므로
# 기본은 비활성이고, 켜면 결과에 차단량이 숫자로 남는다 (CLAUDE.local.md).
# 10,000,000 원/MWh = 10,000 원/kWh 로, 가장 비싼 연료비(유류 수백 원/kWh)의
# 수십 배다. 즉 "발전기를 하나라도 더 돌릴 수 있으면 반드시 돌린다".
DEFAULT_SHED_PENALTY_KRW_PER_MWH = 10_000_000.0

# PuLP status -> 우리 SolveStatus
_STATUS_MAP = {
    pulp.LpStatusOptimal: "optimal",
    pulp.LpStatusInfeasible: "infeasible",
    pulp.LpStatusUnbounded: "error",
    pulp.LpStatusUndefined: "error",
    pulp.LpStatusNotSolved: "error",
}


class SolverUnavailableError(RuntimeError):
    """요청한 solver 를 이 환경에서 쓸 수 없을 때."""


@dataclass(frozen=True)
class SolverConfig:
    """solver 설정. config/settings.yaml 의 solver_* 필드에서 만든다."""

    name: str = "HiGHS"
    msg: bool = False
    time_limit_s: float | None = None
    mip_gap: float = 0.0

    @classmethod
    def from_settings(cls, settings) -> SolverConfig:  # noqa: ANN001 - models.Settings
        return cls(
            name=settings.solver,
            msg=settings.solver_msg,
            time_limit_s=settings.solver_time_limit_s,
            mip_gap=settings.solver_mip_gap,
        )


def build_solver(config: SolverConfig) -> pulp.LpSolver:
    """PuLP solver 객체. 쓸 수 없으면 가용 목록을 알려주며 예외.

    CBC 바이너리는 Apple Silicon 에서 실행되지 않는다 (settings.yaml 주석 참고).
    그래서 "왜 안 되는지"를 메시지에 담는다.
    """
    kwargs: dict[str, object] = {"msg": config.msg}
    if config.time_limit_s:
        kwargs["timeLimit"] = config.time_limit_s
    if config.mip_gap:
        kwargs["gapRel"] = config.mip_gap

    name = config.name.strip().upper()
    factory = {
        "HIGHS": pulp.HiGHS,
        "CBC": pulp.PULP_CBC_CMD,
        "PULP_CBC_CMD": pulp.PULP_CBC_CMD,
    }.get(name)
    if factory is None:
        raise SolverUnavailableError(
            f"solver '{config.name}' 를 모른다. 사용 가능: HiGHS, PULP_CBC_CMD "
            "(CLAUDE.md: MILP 는 PuLP 또는 OR-Tools 만 허용)"
        )

    solver = factory(**kwargs)
    if not solver.available():
        raise SolverUnavailableError(
            f"solver '{config.name}' 가 이 환경에서 실행되지 않는다. "
            f"설치된 solver: {pulp.listSolvers(onlyAvailable=True)}"
        )
    return solver


@dataclass
class _Model:
    """빌드 결과. 테스트에서 모델만 세워 검사할 수 있도록 분리한다."""

    problem: pulp.LpProblem
    u: dict[str, dict[int, pulp.LpVariable]]
    v: dict[str, dict[int, pulp.LpVariable]]
    w: dict[str, dict[int, pulp.LpVariable]]
    p: dict[str, dict[int, pulp.LpVariable]]
    shed: dict[int, pulp.LpVariable]
    charge: dict[int, pulp.LpVariable] | None
    discharge: dict[int, pulp.LpVariable] | None
    soc: dict[int, pulp.LpVariable] | None
    ess: EssSpec | None
    p_max_eff: dict[str, float]
    hydro_factor: float
    notes: list[str]
    build_time_s: float


def build_model(
    inp: DispatchInput,
    *,
    hydro_policy: HydroPolicy | None = None,
    allow_load_shedding: bool = False,
    shed_penalty_krw_per_mwh: float = DEFAULT_SHED_PENALTY_KRW_PER_MWH,
    ess: EssSpec | None = None,
) -> _Model:
    """MILP 를 세운다 (풀지는 않는다).

    `ess` 를 주면 충·방전을 **제약 있는 결정변수**로 같이 푼다 (후처리가 아니다).
    """
    started = time.perf_counter()
    policy = hydro_policy or HydroPolicy()
    hydro_factor = policy.factor(inp.storage_rate_pct)
    notes: list[str] = []

    gens: list[Generator] = list(inp.generators)
    hours = range(inp.horizon_h)
    demand = inp.net_demand_mw
    reserve_ratio = inp.reserve_margin_ratio
    p_max_eff = effective_p_max(gens, hydro_factor)

    if hydro_factor < 1.0:
        notes.append(
            f"저수율 {inp.storage_rate_pct:.1f}% < {policy.threshold_pct:.0f}% — "
            f"수력 상한에 factor {hydro_factor:.3f} 적용 ({policy.mode})"
        )

    problem = pulp.LpProblem("uc_economic_dispatch", pulp.LpMinimize)

    # ── 변수 ────────────────────────────────────────────────────────────────
    u = {g.id: {t: pulp.LpVariable(f"u_{g.id}_{t}", cat=pulp.LpBinary) for t in hours}
         for g in gens}
    v = {g.id: {t: pulp.LpVariable(f"v_{g.id}_{t}", lowBound=0, upBound=1) for t in hours}
         for g in gens}
    w = {g.id: {t: pulp.LpVariable(f"w_{g.id}_{t}", lowBound=0, upBound=1) for t in hours}
         for g in gens}
    p = {g.id: {t: pulp.LpVariable(f"p_{g.id}_{t}", lowBound=0, upBound=p_max_eff[g.id])
                for t in hours}
         for g in gens}

    if allow_load_shedding:
        shed = {t: pulp.LpVariable(f"shed_{t}", lowBound=0, upBound=demand[t]) for t in hours}
        notes.append(
            f"부하차단 슬랙 사용 (페널티 {shed_penalty_krw_per_mwh:,.0f} 원/MWh) — "
            "과제 필수 제약이 아닌 feasibility 처리용 모델링 선택"
        )
    else:
        shed = {t: pulp.LpVariable(f"shed_{t}", lowBound=0, upBound=0) for t in hours}

    # ── ESS 변수 (제약 8) ───────────────────────────────────────────────────
    charge = discharge = soc = None
    peak_var = None
    if ess is not None:
        charge = {t: pulp.LpVariable(f"ch_{t}", lowBound=0, upBound=ess.power_charge_mw)
                  for t in hours}
        discharge = {t: pulp.LpVariable(f"dis_{t}", lowBound=0,
                                        upBound=ess.power_discharge_mw)
                     for t in hours}
        soc = {t: pulp.LpVariable(f"soc_{t}", lowBound=ess.soc_min_mwh,
                                  upBound=ess.soc_max_mwh)
               for t in hours}
        notes.append(
            f"ESS {ess.power_discharge_mw:,.0f} MW / {ess.capacity_mwh:,.0f} MWh, "
            f"왕복효율 {ess.round_trip_efficiency:.2f}, mode={ess.mode}"
        )
        if ess.mode in ("peak_shaving", "combined"):
            peak_var = pulp.LpVariable("peak_mw", lowBound=0)

    def ess_net(t: int):
        """ESS 순출력 (+ 방전 / - 충전). ESS 가 없으면 0."""
        if charge is None or discharge is None:
            return 0
        return discharge[t] - charge[t]

    # ── 목적함수: 연료비 + 기동비 (+ 부하차단 페널티) (+ 피크 가중치) ───────
    objective = (
        pulp.lpSum(
            (1 - g.no_load_frac) * g.fuel_cost_krw_per_mwh * p[g.id][t]
            + g.no_load_frac * g.fuel_cost_krw_per_mwh * g.p_max_mw * u[g.id][t]
            for g in gens for t in hours
        )
        + pulp.lpSum(g.startup_cost_krw * v[g.id][t] for g in gens for t in hours)
        + pulp.lpSum(shed_penalty_krw_per_mwh * shed[t] for t in hours)
    )
    if ess is not None and ess.mode == "combined" and peak_var is not None:
        # [모델링 선택] 비용 + lambda x 피크 의 가중합. lambda 는 config 값이며
        # 비용 단위가 아니라 "피크 1 MW 를 얼마로 볼지"의 선호도다. 그래서
        # 이 모드의 total_cost 는 baseline 과의 비용 비교에 그대로 쓰면 안 된다.
        objective += ess.peak_penalty_krw_per_mw * peak_var
        notes.append(
            f"combined 모드: 목적함수에 피크 가중치 "
            f"{ess.peak_penalty_krw_per_mw:,.0f} 원/MW 가 더해졌다 (모델링 선택)"
        )
    problem += objective, "total_cost_krw"

    # ── 제약 1: 수급 균형 ───────────────────────────────────────────────────
    # ESS 방전은 공급, 충전은 부하다.
    for t in hours:
        problem += (
            pulp.lpSum(p[g.id][t] for g in gens) + shed[t] + ess_net(t) == demand[t],
            f"balance_{t}",
        )

    # ── 제약 2: 예비율 ──────────────────────────────────────────────────────
    # [제안 — 팀 확정 필요] Σ(u · p_max_eff) >= (1 + r) · (순수요 - 부하차단).
    # 즉 "가동 중인 유닛의 상한 합"을 공급능력으로 본다. 정지한 유닛은 그 시간에
    # 즉시 쓸 수 없으므로 제외한다. 부하차단을 쓰면 차단 후 실제 공급한 수요를
    # 기준으로 예비율을 요구한다 (위기 시나리오에서 "차단하고 나면 안정"을 판정).
    # ESS 가 있으면 분모는 발전기가 담당하는 부하(수요 - 차단 + 충전 - 방전)다.
    # ESS 의 남은 방전 여력을 예비력으로 셀지는 [미정] 이라 세지 않는다 (보수적).
    for t in hours:
        problem += (
            pulp.lpSum(p_max_eff[g.id] * u[g.id][t] for g in gens)
            >= (1 + reserve_ratio) * (demand[t] - shed[t] - ess_net(t)),
            f"reserve_{t}",
        )

    for g in gens:
        cap = p_max_eff[g.id]
        su_ramp = startup_ramp_mw(g, cap)
        sd_ramp = shutdown_ramp_mw(g, cap)

        for t in hours:
            # ── 제약 3: 출력 상·하한 ────────────────────────────────────────
            problem += (p[g.id][t] <= cap * u[g.id][t], f"pmax_{g.id}_{t}")
            problem += (p[g.id][t] >= g.p_min_mw * u[g.id][t], f"pmin_{g.id}_{t}")

            # ── 제약 7: 기동/정지 연결식 ────────────────────────────────────
            prev_u = u[g.id][t - 1] if t > 0 else g.u_init
            problem += (
                u[g.id][t] - prev_u == v[g.id][t] - w[g.id][t],
                f"link_{g.id}_{t}",
            )

            # ── 제약 4: 램프율 ──────────────────────────────────────────────
            if t > 0:
                prev_p: object = p[g.id][t - 1]
                prev_on: object = u[g.id][t - 1]
            elif inp.p_init_mw and g.id in inp.p_init_mw:
                prev_p = inp.p_init_mw[g.id]
                prev_on = g.u_init
            else:
                prev_p = None  # t=0 램프 제약 생략 (초기 출력 미지)
                prev_on = None

            if prev_p is not None:
                problem += (
                    p[g.id][t] - prev_p
                    <= g.ramp_up_mw_per_h * prev_on + su_ramp * v[g.id][t],
                    f"rampup_{g.id}_{t}",
                )
                problem += (
                    prev_p - p[g.id][t]
                    <= g.ramp_down_mw_per_h * u[g.id][t] + sd_ramp * w[g.id][t],
                    f"rampdown_{g.id}_{t}",
                )

        # ── 제약 5/6: 최소 기동/정지 시간 ───────────────────────────────────
        # 초기 상태가 최소시간을 못 채웠으면 남은 시간만큼 상태를 고정한다.
        if g.u_init == 1 and g.init_state_h < g.min_up_h:
            for t in range(min(g.min_up_h - g.init_state_h, inp.horizon_h)):
                problem += (u[g.id][t] == 1, f"init_on_{g.id}_{t}")
        if g.u_init == 0 and g.init_state_h < g.min_down_h:
            for t in range(min(g.min_down_h - g.init_state_h, inp.horizon_h)):
                problem += (u[g.id][t] == 0, f"init_off_{g.id}_{t}")

        # 이동 창(Rajan-Takriti): 최근 min_up 시간 안에 기동이 있었으면 가동 중이어야 한다.
        for t in hours:
            if t >= g.min_up_h - 1:
                window = range(t - g.min_up_h + 1, t + 1)
                problem += (
                    pulp.lpSum(v[g.id][k] for k in window) <= u[g.id][t],
                    f"minup_{g.id}_{t}",
                )
            if t >= g.min_down_h - 1:
                window = range(t - g.min_down_h + 1, t + 1)
                problem += (
                    pulp.lpSum(w[g.id][k] for k in window) <= 1 - u[g.id][t],
                    f"mindown_{g.id}_{t}",
                )

    # ── 제약 8: ESS ─────────────────────────────────────────────────────────
    if ess is not None and charge is not None and discharge is not None and soc is not None:
        for t in hours:
            # SOC 전이식. 충전은 효율만큼 덜 쌓이고, 방전은 효율만큼 더 꺼낸다.
            #   soc_t = soc_{t-1} + eta_ch * ch_t * dt - dis_t * dt / eta_dis
            previous = soc[t - 1] if t > 0 else ess.soc_init_mwh
            problem += (
                soc[t] == previous
                + ess.eta_charge * charge[t]
                - discharge[t] / ess.eta_discharge,
                f"soc_{t}",
            )
            if ess.enforce_exclusive:
                # 동시 충·방전 금지. 왕복효율 < 1 이면 손실이라 비용 최소화에서
                # 저절로 배제되지만, 명시적으로 막고 싶을 때 쓴다 (이진변수 +H).
                z = pulp.LpVariable(f"z_{t}", cat=pulp.LpBinary)
                problem += (charge[t] <= ess.power_charge_mw * z, f"ess_ch_excl_{t}")
                problem += (
                    discharge[t] <= ess.power_discharge_mw * (1 - z),
                    f"ess_dis_excl_{t}",
                )

        # 종료 SOC 하한. 없으면 배터리를 비우고 끝내 "공짜 에너지"를 얻는다.
        problem += (
            soc[inp.horizon_h - 1] >= ess.soc_final_min_mwh,
            "soc_final",
        )

        if peak_var is not None:
            # 피크 = max(발전기 부하). 선형화는 "모든 t 에서 peak >= 부하".
            for t in hours:
                problem += (
                    peak_var >= demand[t] - shed[t] - ess_net(t),
                    f"peak_def_{t}",
                )
            if ess.mode == "peak_shaving":
                before = max(demand)
                cap = before * (1 - ess.peak_target_pct / 100.0)
                problem += (peak_var <= cap, "peak_cap")
                notes.append(
                    f"peak_shaving 모드: 피크를 {ess.peak_target_pct:.1f}% 이상 깎도록 "
                    f"상한 {cap:,.0f} MW 를 걸었다 (ESS 미적용 피크 {before:,.0f} MW). "
                    "달성 불가하면 infeasible 로 드러난다"
                )

    if not (inp.p_init_mw and all(g.id in inp.p_init_mw for g in gens)):
        notes.append(
            "유닛별 초기 출력(p_init_mw)이 없어 t=0 의 램프 제약을 생략했다 "
            "(모르는 값을 가정하면 첫 시간이 인위적으로 묶인다)"
        )

    return _Model(
        problem=problem,
        u=u, v=v, w=w, p=p, shed=shed,
        charge=charge, discharge=discharge, soc=soc, ess=ess,
        p_max_eff=p_max_eff,
        hydro_factor=hydro_factor,
        notes=notes,
        build_time_s=time.perf_counter() - started,
    )


def _model_size(model: _Model, solve_time_s: float) -> ModelSize:
    variables = model.problem.variables()
    return ModelSize(
        n_variables=len(variables),
        # PuLP 는 cat=Binary 를 0~1 정수로 저장하므로 cat 비교로는 못 센다.
        n_binaries=sum(1 for x in variables if x.isBinary()),
        n_constraints=len(model.problem.constraints),
        build_time_s=model.build_time_s,
        solve_time_s=solve_time_s,
    )


def _status(raw: int, solve_time_s: float, config: SolverConfig) -> str:
    """PuLP status 를 우리 상태값으로. time limit 에 걸린 경우를 구분한다."""
    mapped = _STATUS_MAP.get(raw, "error")
    if mapped == "error" and config.time_limit_s and solve_time_s >= config.time_limit_s * 0.95:
        return "time_limit"
    return mapped


def solve(
    inp: DispatchInput,
    *,
    solver_config: SolverConfig | None = None,
    hydro_policy: HydroPolicy | None = None,
    allow_load_shedding: bool = False,
    shed_penalty_krw_per_mwh: float = DEFAULT_SHED_PENALTY_KRW_PER_MWH,
    ess: EssSpec | None = None,
) -> DispatchResult:
    """UC + ED MILP 를 풀어 급전 계획을 돌려준다.

    status 가 "optimal" 이 아니면 급전값은 비어 있다(전부 0). 억지로 성공 응답을
    만들지 않는다 (CLAUDE.local.md infeasible 처리). 호출자는 status 를 먼저 본다.
    """
    config = solver_config or SolverConfig()
    model = build_model(
        inp,
        hydro_policy=hydro_policy,
        allow_load_shedding=allow_load_shedding,
        shed_penalty_krw_per_mwh=shed_penalty_krw_per_mwh,
        ess=ess,
    )

    solver = build_solver(config)
    started = time.perf_counter()
    raw_status = model.problem.solve(solver)
    solve_time_s = time.perf_counter() - started
    status = _status(raw_status, solve_time_s, config)
    size = _model_size(model, solve_time_s)

    logger.info(
        "MILP %s: 유닛 %d x %dh, 변수 %d (이진 %d), 제약 %d, build %.2fs, solve %.2fs",
        status, len(inp.generators), inp.horizon_h,
        size.n_variables, size.n_binaries, size.n_constraints,
        size.build_time_s, size.solve_time_s,
    )

    def value(var: pulp.LpVariable) -> float:
        raw = var.value()
        return 0.0 if raw is None else float(raw)

    solved = status in ("optimal", "feasible", "time_limit")
    on = {
        g.id: [bool(round(value(model.u[g.id][t]))) if solved else False
               for t in range(inp.horizon_h)]
        for g in inp.generators
    }
    p_mw = {
        g.id: [value(model.p[g.id][t]) if solved else 0.0 for t in range(inp.horizon_h)]
        for g in inp.generators
    }
    shed_mw = [value(model.shed[t]) if solved else 0.0 for t in range(inp.horizon_h)]

    ess_schedule = None
    if ess is not None and model.charge is not None and model.discharge is not None:
        ess_schedule = EssSchedule(
            charge_mw=[value(model.charge[t]) if solved else 0.0
                       for t in range(inp.horizon_h)],
            discharge_mw=[value(model.discharge[t]) if solved else 0.0
                          for t in range(inp.horizon_h)],
            soc_mwh=[value(model.soc[t]) if solved else ess.soc_init_mwh
                     for t in range(inp.horizon_h)],
            spec=ess,
        )

    notes = list(model.notes)
    if not solved:
        notes.append(
            f"solver status '{status}' — 급전값은 유효하지 않다. "
            "진단 순서: 용량 vs 수요+예비 -> 초기상태+min up/down -> ramp -> "
            "Pmin 과잉발전 -> 수력 제한 -> 단위 오류"
        )
    objective = model.problem.objective.value() if solved else None

    result = assemble(
        "milp", status, inp, on, p_mw, model.p_max_eff, shed_mw,
        shed_penalty_krw_per_mwh=shed_penalty_krw_per_mwh,
        hydro_factor=model.hydro_factor,
        ess=ess_schedule,
        size=size,
        notes=notes,
    )

    # 교차검증: 목적함수 값과 다시 계산한 비용이 어긋나면 계수 누락이다.
    # combined 모드는 목적함수에 비용이 아닌 피크 가중치가 섞여 있어 제외한다.
    if ess is not None and ess.mode == "combined":
        objective = None
    if objective is not None and abs(objective - result.total_cost_krw) > max(
        1.0, abs(objective) * 1e-6
    ):
        logger.warning(
            "목적함수 %.1f 와 재계산 비용 %.1f 가 다르다 — 비용 계수를 확인할 것",
            objective, result.total_cost_krw,
        )
    return result
