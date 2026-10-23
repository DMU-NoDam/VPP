"""Two-Stage Stochastic 급전 + VSS (docs/formulation.md 3장).

1단계(Day-ahead, 시나리오 공통): 가동 u · 기동 y · 정지 z.
2단계(Real-time, 시나리오별):   출력 P[i,t,s], 당일 추가 가동 u^RT · 기동 y^RT, 부하차단 L[t,s].

목적함수 (3장 v0.10):
    min Σ SU_i·y[i,t] + Σ_s p_s Σ_t ( Σ_i c_i·P[i,t,s] + Σ_i SU_i·y^RT[i,t,s] + K·L[t,s] )

제약 (3장 S1~S6, R1~R4, 2장 제약 3·4·5·기동 논리):
    S1  Σ_i P[i,t,s] + L[t,s] >= d[s,t]
    S2  Pmin_i·o[i,t,s] <= P[i,t,s] <= Pmax_i·o[i,t,s],  o = u + u^RT
    R1  u^RT[i,t,s] 는 t >= ceil(T^SU_i) 일 때만 존재 (config/stochastic.yaml 의 기동시간)
    R2  u[i,t] + u^RT[i,t,s] <= 1
    R3  y^RT[i,t,s] >= u^RT[i,t,s] - u^RT[i,t-1,s]
    R4  당일 기동 후 최소 가동 TU_i
    S3  |P[i,t,s] - P[i,t-1,s]| <= RampRate_i
    S4  최소 가동/정지 시간, y[i,t] - z[i,t] = u[i,t] - u[i,t-1]   (1단계 변수에만)
    S6  d[s,t] = D[s,t] - 태양광[s,t] - 풍력[s,t]

문서에 없거나 미확정이라 코드가 정한 것 (가정):
    S5 예비율   1단계에 둔다. Σ Pmax_i·u[i,t] + q[t] >= (1 + r)·(기대 순수요). 부족분 q 에
                페널티를 붙여 설비가 모자란 날에도 해가 나오게 한다.
    초기 상태   u[i,-1] = generators.yaml 의 초기 가동상태. 최소 가동/정지 시간을 아직 못 채웠으면
                남은 시간만큼 그 상태를 유지한다.
    램프        출력 증가에는 ramp_up, 감소에는 ramp_down 을 쓴다. 0시의 직전 출력은 모르므로
                0시에는 램프를 걸지 않는다.
    VSS%        분모는 EEV.
    당일 기동   기동시간이 config 에 없는 연료(유류 등)는 당일 기동 불가. 당일 정지는 없다.
                1단계에서 끈 유닛을 당일 다시 켤 때 최소 정지 시간은 보지 않는다.

확장형(Deterministic Equivalent) 하나를 Pyomo 로 세워 HiGHS 로 푼다. 같은 빌더로
RP(전 시나리오) · EV(평균 시나리오 1개) · EEV(EV 의 1단계 고정) · WS(시나리오별 완전정보)
네 문제를 풀어 VSS = EEV - RP, EVPI = RP - WS 를 낸다.

발전기 제원은 units.Unit 만 본다 (C 의 generators.yaml 형식).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pyomo.environ as pyo
import yaml
from pyomo.contrib.solver.common.factory import SolverFactory
from pyomo.contrib.solver.common.results import SolutionStatus, TerminationCondition

from shared.schemas import (
    CostBreakdown,
    HourSummary,
    Scenario,
    ScenarioResult,
    SolverOptions,
    StochasticResponse,
    UnitHour,
    VssReport,
)
from units import Unit, find_file

VSS_TARGET_PCT = 3.0
# 목적함수를 백만 원 단위로 푼다. 원 단위(1e10)로 두면 HiGHS 수치 조건이 나빠져 느려진다.
OBJ_SCALE = 1e-6
FIRST_STAGE_VARS = ("u", "y", "z")
# Pyomo 의 HiGHS 래퍼는 풀 때마다 프로세스 전체의 stdout(fd 1)을 바꿔 끼운다 (capture_fd=True).
# FastAPI 스레드풀에서 두 요청이 동시에 풀면 바꿔 끼우기가 엇갈려 stdout 이 막히고, uvicorn 로그
# 쓰기가 멈춰 다른 요청까지 응답하지 못한다. 그래서 풀이는 한 번에 하나만 한다.
_SOLVE_LOCK = threading.Lock()


@dataclass(frozen=True)
class StochasticParams:
    """config/stochastic.yaml. 기동시간은 3장 비가역성 규칙 표, 나머지는 가정 항의 값."""

    reserve_short_penalty_krw_per_mw: float = 1_000_000.0
    # 연료별 기동시간 T^SU (h). 없는 연료는 당일 기동 불가 (가정 — 유류는 3장에서 미확정)
    startup_time_h: dict[str, float] = field(default_factory=dict)

    def rt_start_hour(self, unit: Unit) -> int | None:
        """R1: 당일 추가 기동이 가능한 첫 시각 ceil(T^SU). 불가면 None."""
        t_su = self.startup_time_h.get(unit.fuel)
        return None if t_su is None else math.ceil(t_su)


def load_params(path: Path | None = None) -> StochasticParams:
    path = path or find_file("STOCHASTIC_YAML", "config", "stochastic.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    known = StochasticParams.__dataclass_fields__
    return StochasticParams(**{k: v for k, v in raw.items() if k in known})


@dataclass
class Solved:
    """한 번 푼 결과. 값은 numpy 배열 (유닛 x 시간 [x 시나리오])."""

    status: str
    objective: float
    solve_time_s: float
    first: dict[str, np.ndarray]
    second: dict[str, np.ndarray]


def mean_scenario(scenarios: list[Scenario]) -> Scenario:
    """확률 가중 평균 시나리오 1개 (EV 문제의 입력)."""
    w = np.array([s.probability for s in scenarios])
    w = w / w.sum()

    def avg(field: str) -> list[float]:
        return list(np.average([getattr(s, field) for s in scenarios], axis=0, weights=w))

    return Scenario(
        scenario_id="mean", probability=1.0,
        demand_mw=avg("demand_mw"), solar_mw=avg("solar_mw"), wind_mw=avg("wind_mw"),
    )


def net_load(s: Scenario) -> np.ndarray:
    """S6: 순수요 d = 수요 - 태양광 - 풍력."""
    return np.array(s.demand_mw) - np.array(s.solar_mw) - np.array(s.wind_mw)


# ── 모델 ─────────────────────────────────────────────────────────────────────


def build_model(
    units: list[Unit],
    scenarios: list[Scenario],
    params: StochasticParams,
    reserve_ratio: float,
    shed_penalty: float,
    fixed: dict[str, np.ndarray] | None = None,
    plan_net: np.ndarray | None = None,
) -> pyo.ConcreteModel:
    """확장형 모델 (formulation.md 3장). fixed 를 주면 1단계 변수를 그 값으로 고정한다 (EEV).

    plan_net 은 예비율 제약(가정 — S5 는 문서에서 미확정)의 기준 순수요다. RP·EV·EEV·WS 가 같은
    값을 써야 1단계 실현가능영역이 같아져 WS <= RP <= EEV 가 성립한다. None 이면 입력
    시나리오의 기대 순수요.
    """
    horizon = len(scenarios[0].demand_mw)
    last = horizon - 1  # N_T
    gen_ix, hour_ix, scen_ix = range(len(units)), range(horizon), range(len(scenarios))

    prob = np.array([s.probability for s in scenarios])
    prob = prob / prob.sum()
    net = np.array([net_load(s) for s in scenarios])  # d[s,t]
    net_expected = prob @ net if plan_net is None else np.asarray(plan_net, dtype=float)

    m = pyo.ConcreteModel()
    m.G = pyo.Set(initialize=gen_ix)
    m.T = pyo.Set(initialize=hour_ix)
    m.S = pyo.Set(initialize=scen_ix)

    # 1단계
    m.u = pyo.Var(m.G, m.T, domain=pyo.Binary)
    m.y = pyo.Var(m.G, m.T, domain=pyo.Binary)  # 기동
    m.z = pyo.Var(m.G, m.T, domain=pyo.Binary)  # 정지
    m.res_short = pyo.Var(m.T, domain=pyo.NonNegativeReals)  # 가정: 예비력 부족 슬랙 q

    # 2단계
    m.p = pyo.Var(m.G, m.T, m.S, domain=pyo.NonNegativeReals)
    m.shed = pyo.Var(m.T, m.S, domain=pyo.NonNegativeReals)  # L
    # R1: 당일 추가 기동이 허용된 (유닛, 시각) 에만 u^RT, y^RT 를 만든다
    rt_from = {g: params.rt_start_hour(units[g]) for g in gen_ix}
    m.RT = pyo.Set(
        dimen=2,
        initialize=[(g, t) for g in gen_ix if rt_from[g] is not None
                    for t in hour_ix if t >= rt_from[g]],
    )
    m.urt = pyo.Var(m.RT, m.S, domain=pyo.Binary)
    m.yrt = pyo.Var(m.RT, m.S, domain=pyo.Binary)

    def on(g: int, t: int, s: int) -> pyo.Expression:
        """R2: 실제 가동 상태 o = u + u^RT."""
        return m.u[g, t] + m.urt[g, t, s] if (g, t) in m.RT else m.u[g, t]

    c = pyo.ConstraintList()
    m.cons = c

    for g in gen_ix:
        unit = units[g]
        # 가정: 초기 상태가 최소 가동/정지 시간을 아직 못 채웠으면 그만큼 상태를 유지한다
        if unit.u_init:
            keep = max(unit.min_up_h - unit.init_state_h, 0)
        else:
            keep = max(unit.min_down_h - unit.init_state_h, 0)
        for t in hour_ix:
            prev_u = m.u[g, t - 1] if t > 0 else unit.u_init
            # 기동 논리 (2장, 논문 식 (4))
            c.add(m.y[g, t] - m.z[g, t] == m.u[g, t] - prev_u)
            if t < keep:
                c.add(m.u[g, t] == unit.u_init)
            # 제약 5 (논문 식 (2)(3)): t 에 켜지면 TU 시간, 꺼지면 TD 시간 유지
            for k in range(t + 1, min(t + unit.min_up_h - 1, last) + 1):
                c.add(m.u[g, k] >= m.u[g, t] - prev_u)
            for k in range(t + 1, min(t + unit.min_down_h - 1, last) + 1):
                c.add(1 - m.u[g, k] >= prev_u - m.u[g, t])

    for t in hour_ix:
        # 가정 (S5 미확정): 예비율을 1단계에 둔다
        c.add(
            sum(units[g].p_max_mw * m.u[g, t] for g in gen_ix) + m.res_short[t]
            >= (1 + reserve_ratio) * float(net_expected[t])
        )

    for s in scen_ix:
        for g in gen_ix:
            unit = units[g]
            for t in hour_ix:
                # S2 (v0.10: u 대신 실제 가동 상태 o)
                c.add(m.p[g, t, s] >= unit.p_min_mw * on(g, t, s))
                c.add(m.p[g, t, s] <= unit.p_max_mw * on(g, t, s))
                if (g, t) in m.RT:
                    prev_rt = m.urt[g, t - 1, s] if (g, t - 1) in m.RT else 0
                    # R2: 1단계에서 켠 유닛은 당일 또 켤 수 없다 (o <= 1)
                    c.add(m.u[g, t] + m.urt[g, t, s] <= 1)
                    # R3: 당일 기동 논리
                    c.add(m.yrt[g, t, s] >= m.urt[g, t, s] - prev_rt)
                    # R4: 당일 기동 후 최소 가동 (k 는 t 이후라 모두 RT 안에 있다)
                    for k in range(t + 1, min(t + unit.min_up_h - 1, last) + 1):
                        c.add(m.urt[g, k, s] >= m.urt[g, t, s] - prev_rt)
                # S3
                if t > 0:
                    c.add(m.p[g, t, s] - m.p[g, t - 1, s] <= unit.ramp_up_mw_per_h)
                    c.add(m.p[g, t - 1, s] - m.p[g, t, s] <= unit.ramp_down_mw_per_h)
        for t in hour_ix:
            # S1
            c.add(sum(m.p[g, t, s] for g in gen_ix) + m.shed[t, s] >= float(net[s, t]))

    if fixed is not None:
        for name in FIRST_STAGE_VARS:
            var = getattr(m, name)
            for g in gen_ix:
                for t in hour_ix:
                    var[g, t].fix(round(float(fixed[name][g, t])))

    first_cost = sum(units[g].startup_cost_krw * m.y[g, t] for g in gen_ix for t in hour_ix)
    # 가정: 예비력 부족 페널티. 문서 3장 목적함수에는 없는 항이다
    reserve_pen = sum(params.reserve_short_penalty_krw_per_mw * m.res_short[t] for t in hour_ix)

    def second_cost(s: int) -> pyo.Expression:
        fuel = sum(units[g].fuel_cost_krw_per_mwh * m.p[g, t, s] for g in gen_ix for t in hour_ix)
        rt_start = sum(units[g].startup_cost_krw * m.yrt[g, t, s] for g, t in m.RT)
        return fuel + rt_start + sum(shed_penalty * m.shed[t, s] for t in hour_ix)

    m.obj = pyo.Objective(
        expr=OBJ_SCALE
        * (first_cost + reserve_pen + sum(float(prob[s]) * second_cost(s) for s in scen_ix)),
        sense=pyo.minimize,
    )
    m._units, m._prob, m._net = units, prob, net
    return m


def solve(m: pyo.ConcreteModel, opts: SolverOptions) -> Solved:
    """HiGHS 로 풀고 변수 값을 배열로 꺼낸다."""
    with _SOLVE_LOCK:
        t0 = time.perf_counter()
        res = SolverFactory("highs").solve(
            m,
            time_limit=opts.time_limit_s,
            rel_gap=opts.mip_gap,
            load_solutions=False,
            raise_exception_on_nonoptimal_result=False,
        )
        elapsed = time.perf_counter() - t0

    tc = res.termination_condition
    if res.solution_status == SolutionStatus.noSolution:
        status = "infeasible" if tc == TerminationCondition.provenInfeasible else "error"
        return Solved(status, float("nan"), elapsed, {}, {})
    res.solution_loader.load_vars()
    if tc == TerminationCondition.convergenceCriteriaSatisfied:
        status = "optimal"
    elif tc == TerminationCondition.maxTimeLimit:
        status = "time_limit"
    else:
        status = "feasible"

    gen_ix, hour_ix, scen_ix = list(m.G), list(m.T), list(m.S)

    first = {
        name: np.round([[pyo.value(getattr(m, name)[g, t]) for t in hour_ix] for g in gen_ix])
        for name in FIRST_STAGE_VARS
    }
    first["res_short"] = np.array([pyo.value(m.res_short[t]) for t in hour_ix])
    second = {
        "p": np.array(
            [[[pyo.value(m.p[g, t, s]) for s in scen_ix] for t in hour_ix] for g in gen_ix]
        ),
        "shed": np.array([[pyo.value(m.shed[t, s]) for s in scen_ix] for t in hour_ix]),
        "urt": np.zeros((len(gen_ix), len(hour_ix), len(scen_ix))),
        "yrt": np.zeros((len(gen_ix), len(hour_ix), len(scen_ix))),
    }
    for g, t in m.RT:
        for s in scen_ix:
            second["urt"][g, t, s] = round(pyo.value(m.urt[g, t, s]))
            second["yrt"][g, t, s] = round(pyo.value(m.yrt[g, t, s]))
    return Solved(status, float(pyo.value(m.obj)) / OBJ_SCALE, elapsed, first, second)


# ── 비용 분해 ────────────────────────────────────────────────────────────────


def scenario_costs(
    units: list[Unit], params: StochasticParams, shed_penalty: float, sol: Solved
) -> list[CostBreakdown]:
    """시나리오별 비용. 1단계 기동비와 예비력 부족 페널티는 모든 시나리오에 똑같이 붙인다.

    startup_won = 1단계 기동비 + 그 시나리오의 당일 추가 기동비.
    """
    fuel = np.array([u.fuel_cost_krw_per_mwh for u in units])
    su = np.array([u.startup_cost_krw for u in units])
    first_startup = float(su @ sol.first["y"].sum(axis=1))
    # 가정 항(예비력 부족 페널티)은 recourse_won 자리에 보고한다. 3장 모델에는 다른 조정 비용이 없다
    reserve_pen = params.reserve_short_penalty_krw_per_mw * float(sol.first["res_short"].sum())
    out = []
    for s in range(sol.second["p"].shape[2]):
        fuel_won = float(fuel @ sol.second["p"][:, :, s].sum(axis=1))
        startup = first_startup + float(su @ sol.second["yrt"][:, :, s].sum(axis=1))
        shed_won = shed_penalty * float(sol.second["shed"][:, s].sum())
        out.append(
            CostBreakdown(
                fuel_won=fuel_won, startup_won=startup, recourse_won=reserve_pen,
                shed_won=shed_won, total_won=fuel_won + startup + reserve_pen + shed_won,
            )
        )
    return out


def expected(costs: list[CostBreakdown], prob: np.ndarray) -> CostBreakdown:
    fields = CostBreakdown.model_fields
    return CostBreakdown(
        **{f: float(sum(p * getattr(c, f) for p, c in zip(prob, costs))) for f in fields}
    )


def hour_summaries(
    units: list[Unit], scenario: Scenario, sol: Solved, s: int, start: datetime
) -> list[HourSummary]:
    co2 = np.array([u.co2_ton_per_mwh for u in units])
    p_max = np.array([u.p_max_mw for u in units])
    net = net_load(scenario)
    out = []
    for t, d in enumerate(scenario.demand_mw):
        thermal = float(sol.second["p"][:, t, s].sum())
        shed = float(sol.second["shed"][t, s])
        # S1 이 부등호라 최소출력 때문에 남는 전력이 생길 수 있다. 그만큼은 재생 출력 제한으로 본다
        surplus = max(thermal + shed - float(net[t]), 0.0)
        on = sol.first["u"][:, t] + sol.second["urt"][:, t, s]
        capacity = float(p_max @ on) + scenario.solar_mw[t] + scenario.wind_mw[t]
        out.append(
            HourSummary(
                timestamp=start + timedelta(hours=t),
                demand_mw=d,
                supply_mw=thermal + scenario.solar_mw[t] + scenario.wind_mw[t] - surplus,
                reserve_pct=(capacity - d) / d * 100,
                shed_mw=shed,
                ess_mw=0.0,
                co2_t=float(co2 @ sol.second["p"][:, t, s]),
            )
        )
    return out


def average_hours(hours: list[list[HourSummary]], prob: np.ndarray) -> list[HourSummary]:
    fields = [f for f in HourSummary.model_fields if f != "timestamp"]
    return [
        HourSummary(
            timestamp=rows[0].timestamp,
            **{f: float(sum(p * getattr(r, f) for p, r in zip(prob, rows))) for f in fields},
        )
        for rows in zip(*hours)
    ]


# ── 진입점 ───────────────────────────────────────────────────────────────────


def run(
    units: list[Unit],
    scenarios: list[Scenario],
    params: StochasticParams,
    start: datetime,
    reserve_ratio: float = 0.10,
    shed_penalty: float = 10_000_000.0,
    opts: SolverOptions | None = None,
    compute_ws: bool = True,
    include_scenario_hours: bool = False,
) -> StochasticResponse:
    """RP · EV · EEV (· WS) 를 차례로 풀어 급전 계획과 VSS 를 돌려준다."""
    opts = opts or SolverOptions()
    t0 = time.perf_counter()

    plan_net = net_load(mean_scenario(scenarios))

    def solve_on(scs: list[Scenario], fixed: dict | None = None) -> Solved:
        model = build_model(units, scs, params, reserve_ratio, shed_penalty, fixed, plan_net)
        return solve(model, opts)

    rp = solve_on(scenarios)
    ev = solve_on([mean_scenario(scenarios)])
    eev = solve_on(scenarios, fixed=ev.first) if ev.first else ev
    ws = None
    if compute_ws:
        ws_runs = [solve_on([s.model_copy(update={"probability": 1.0})]) for s in scenarios]
        if all(r.first for r in ws_runs):
            prob = np.array([s.probability for s in scenarios])
            ws = float(prob / prob.sum() @ np.array([r.objective for r in ws_runs]))

    prob = np.array([s.probability for s in scenarios])
    prob = prob / prob.sum()
    if not rp.first:
        raise RuntimeError(f"RP 를 풀지 못했다: {rp.status}")

    costs = scenario_costs(units, params, shed_penalty, rp)
    hours = [hour_summaries(units, sc, rp, s, start) for s, sc in enumerate(scenarios)]
    # 1단계에는 출력 변수가 없다. 유닛별 대표 출력으로 기대 출력 Σ_s p_s·P[i,t,s] 를 쓴다
    p_expected = rp.second["p"] @ prob
    results = []
    for s, sc in enumerate(scenarios):
        results.append(
            ScenarioResult(
                scenario_id=sc.scenario_id,
                probability=float(prob[s]),
                cost=costs[s],
                shed_mwh=float(rp.second["shed"][:, s].sum()),
                rt_adjust_mwh=float(np.abs(rp.second["p"][:, :, s] - p_expected).sum()),
                hours=hours[s] if include_scenario_hours else None,
            )
        )

    first_stage = [
        UnitHour(
            timestamp=start + timedelta(hours=t),
            unit_id=units[g].id,
            on=bool(rp.first["u"][g, t]),
            p_mw=float(p_expected[g, t]),
        )
        for g in range(len(units))
        for t in range(rp.first["u"].shape[1])
    ]

    vss_won = eev.objective - rp.objective
    # 가정: VSS% 의 분모는 EEV (문서에서 [검증 필요])
    vss_pct = vss_won / eev.objective * 100 if eev.objective else 0.0
    statuses = [rp.status, ev.status, eev.status]
    status = "optimal" if all(s == "optimal" for s in statuses) else rp.status

    return StochasticResponse(
        status=status,
        solve_time_s=time.perf_counter() - t0,
        start=start,
        horizon_h=len(scenarios[0].demand_mw),
        n_scenarios=len(scenarios),
        first_stage=first_stage,
        expected_cost=expected(costs, prob),
        expected_hours=average_hours(hours, prob),
        scenarios=results,
        vss=VssReport(
            rp_won=rp.objective,
            ev_won=ev.objective,
            eev_won=eev.objective,
            vss_won=vss_won,
            vss_pct=vss_pct,
            ws_won=ws,
            evpi_won=rp.objective - ws if ws is not None else None,
            target_met=vss_pct >= VSS_TARGET_PCT,
        ),
    )
