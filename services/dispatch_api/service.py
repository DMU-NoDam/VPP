"""dispatch_api 유스케이스 계층.

FastAPI route 는 검증·호출·직렬화만 한다 (CLAUDE.md 코드 규칙). 여기서
config/데이터 로딩 -> 최적화 -> 제약 재검증 -> 응답 조립을 순서대로 한다.

데이터 경로는 환경변수로 바꿀 수 있다 (docker-compose 는 볼륨으로 마운트한다).
  VPP_CONFIG_DIR  기본 config
  VPP_CSV_DIR     기본 data/csv
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import replace
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import baseline
import carbon
import milp
import pareto
import validation
from loaders.config_loader import (
    ConfigError,
    load_ess,
    load_generator_specs,
    load_settings,
    resolve_fuel_costs,
)
from loaders.data_loader import DataError, build_dispatch_input, read_csv_dataset
from models import DispatchInput, DispatchResult, EssSpec, Generator, Settings
from schedule import cost_reduction_pct, merit_order
from shared.schemas import (
    BaselineComparison,
    CarbonSummary,
    CarbonUnitOperation,
    CheckItem,
    CostBreakdown,
    EssSummary,
    HourSummary,
    MeritOrderEntry,
    MilpRequest,
    MilpResponse,
    ModelSizeOut,
    ParetoPoint,
    ParetoRequest,
    ParetoResponse,
    UnitHour,
)

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.getenv("VPP_CONFIG_DIR", "config"))
CSV_DIR = Path(os.getenv("VPP_CSV_DIR", "data/csv"))

# KPI 기준 (CLAUDE.md 정량 기준). 응답의 target_met 판정에 쓴다.
COST_REDUCTION_TARGET_PCT = 10.0
PEAK_REDUCTION_TARGET_PCT = 5.0

# baseline 규칙을 응답에 그대로 싣는다. "규칙을 문서로 명확히 고정한다"
# (CLAUDE.local.md) 를 코드에서도 보이게 하려는 것이다. baseline.py 의
# 모듈 docstring 과 같은 내용이다.
BASELINE_RULES: tuple[str, ...] = (
    "R1 우선순위: 연료비 오름차순 (동률이면 큰 유닛 먼저)",
    "R2 시야: 매 시간 독립 결정, lookahead 없음",
    "R3 기동: 그 시간의 (1+r)x수요를 채울 때까지 우선순위대로 투입",
    "R4 정지: 빼도 예비율이 유지되고 최소시간·램프가 허용하면 비싼 유닛부터 뺀다",
    "R5 급전: 투입된 유닛을 우선순위대로 상한까지 채운다",
    "R6 ESS·예측 사용 없음. 비용 회계는 MILP 와 동일 (연료비 + 기동비)",
)


def _stamp(path: Path) -> tuple[str, float, int]:
    """파일 변경 감지용 키. 파일이 바뀌면 캐시가 자동으로 갱신된다."""
    stat = path.stat()
    return (str(path), stat.st_mtime, stat.st_size)


@lru_cache(maxsize=8)
def _settings_cached(stamp: tuple[str, float, int]) -> Settings:
    return load_settings(stamp[0])


@lru_cache(maxsize=8)
def _specs_cached(stamp: tuple[str, float, int]):
    return load_generator_specs(stamp[0])


@lru_cache(maxsize=8)
def _ess_cached(stamp: tuple[str, float, int]) -> EssSpec:
    return load_ess(stamp[0])


@lru_cache(maxsize=16)
def _csv_cached(stamp: tuple[str, float, int]) -> tuple[dict, ...]:
    """CSV 를 행 목록으로 캐시한다. 요청마다 다시 읽으면 10초 예산을 깎는다."""
    return tuple(read_csv_dataset(Path(stamp[0]).parent, Path(stamp[0]).stem))


def settings() -> Settings:
    return _settings_cached(_stamp(CONFIG_DIR / "settings.yaml"))


def generators(cfg: Settings) -> list[Generator]:
    """연료비까지 결합된 발전기 목록."""
    specs = _specs_cached(_stamp(CONFIG_DIR / "generators.yaml"))
    fuel_cost = _csv_cached(_stamp(CSV_DIR / "fuel_cost.csv"))
    return resolve_fuel_costs(list(specs), [dict(r) for r in fuel_cost], cfg.fuel_cost_month)


def ess_spec(req: MilpRequest) -> EssSpec | None:
    """요청과 ess.yaml 로 ESS 사양을 정한다. use_ess: false 면 None."""
    if not req.use_ess:
        return None
    spec = _ess_cached(_stamp(CONFIG_DIR / "ess.yaml"))
    if req.ess_mode is not None:
        spec = replace(spec, mode=req.ess_mode)
    if req.ess_peak_target_pct is not None:
        spec = replace(spec, peak_target_pct=req.ess_peak_target_pct)
    return spec


def _dataset(name: str) -> list[dict]:
    return [dict(r) for r in _csv_cached(_stamp(CSV_DIR / f"{name}.csv"))]


def _hourly_timestamps(start: datetime | None, horizon_h: int, timezone: str) -> list[datetime]:
    """net_demand_mw 를 직접 받을 때 쓸 시각 배열.

    start 를 주지 않으면 "지금 시(時)"부터 시작한다 (tz-aware, Asia/Seoul).
    """
    tz = ZoneInfo(timezone)
    first = start.astimezone(tz) if start else datetime.now(tz).replace(
        minute=0, second=0, microsecond=0
    )
    return [first + timedelta(hours=h) for h in range(horizon_h)]


def build_input(req: MilpRequest, cfg: Settings, fleet: list[Generator]) -> DispatchInput:
    """요청 -> DispatchInput. 수요를 직접 받았는지에 따라 경로가 갈린다."""
    units = fleet
    if req.unit_ids is not None:
        chosen = set(req.unit_ids)
        units = [g for g in fleet if g.id in chosen]
        unknown = sorted(chosen - {g.id for g in units})
        if unknown:
            raise ConfigError(f"generators.yaml 에 없는 unit_ids: {unknown}")
        if not units:
            raise ConfigError("unit_ids 가 비어 있다 — 발전기 없이는 풀 수 없다")

    reserve = cfg.reserve_margin_ratio if req.reserve_ratio is None else req.reserve_ratio

    if req.net_demand_mw is not None:
        if len(req.net_demand_mw) != req.horizon_h:
            raise DataError(
                f"net_demand_mw 길이 {len(req.net_demand_mw)} != horizon_h {req.horizon_h}"
            )
        return DispatchInput(
            generators=units,
            timestamps=_hourly_timestamps(req.start, req.horizon_h, cfg.timezone),
            net_demand_mw=[float(v) for v in req.net_demand_mw],
            reserve_margin_ratio=reserve,
            storage_rate_pct=req.storage_rate_pct,
            p_init_mw=req.p_init_mw,
        )

    data_input = build_dispatch_input(
        units,
        cfg,
        _dataset("power_demand"),
        _dataset("trading_by_fuel"),
        _dataset("dam_status"),
        horizon_h=req.horizon_h,
    )
    return DispatchInput(
        generators=units,
        timestamps=data_input.timestamps,
        net_demand_mw=data_input.net_demand_mw,
        reserve_margin_ratio=reserve,
        storage_rate_pct=(
            data_input.storage_rate_pct if req.storage_rate_pct is None
            else req.storage_rate_pct
        ),
        p_init_mw=req.p_init_mw,
    )


def _solver_config(req: MilpRequest, cfg: Settings) -> milp.SolverConfig:
    base = milp.SolverConfig.from_settings(cfg)
    if req.solver is None:
        return base
    return milp.SolverConfig(
        name=base.name,
        msg=base.msg,
        time_limit_s=req.solver.time_limit_s,
        mip_gap=req.solver.mip_gap,
    )


def _hours(result: DispatchResult) -> list[HourSummary]:
    co2 = carbon.hourly_ton(result.units)
    return [
        HourSummary(
            timestamp=result.timestamps[t],
            demand_mw=result.net_demand_mw[t],
            supply_mw=result.supply_mw(t),
            reserve_pct=result.reserve_pct(t),
            shed_mw=result.shed_mw[t],
            ess_mw=result.ess_net_mw(t),  # + 방전 / - 충전
            co2_t=co2[t],
        )
        for t in range(result.horizon_h)
    ]


def _units(result: DispatchResult) -> list[UnitHour]:
    return [
        UnitHour(
            timestamp=result.timestamps[t],
            unit_id=unit.unit_id,
            on=unit.on[t],
            p_mw=unit.p_mw[t],
        )
        for unit in result.units
        for t in range(result.horizon_h)
    ]


def _ess(result: DispatchResult) -> EssSummary | None:
    ess = result.ess
    if ess is None:
        return None

    spec = ess.spec
    reduction = result.peak_reduction_pct
    target = spec.peak_target_pct or PEAK_REDUCTION_TARGET_PCT
    return EssSummary(
        mode=spec.mode,
        power_charge_mw=spec.power_charge_mw,
        power_discharge_mw=spec.power_discharge_mw,
        capacity_mwh=spec.capacity_mwh,
        round_trip_efficiency=spec.round_trip_efficiency,
        soc_min_mwh=spec.soc_min_mwh,
        soc_max_mwh=spec.soc_max_mwh,
        soc_init_mwh=spec.soc_init_mwh,
        charge_mw=ess.charge_mw,
        discharge_mw=ess.discharge_mw,
        soc_mwh=ess.soc_mwh,
        soc_pct=[v / spec.capacity_mwh * 100.0 for v in ess.soc_mwh],
        energy_charged_mwh=ess.energy_charged_mwh,
        energy_discharged_mwh=ess.energy_discharged_mwh,
        loss_mwh=ess.loss_mwh,
        cycles=ess.cycles,
        peak_before_mw=result.peak_demand_mw,
        peak_after_mw=result.peak_after_ess_mw,
        peak_reduction_pct=reduction,
        peak_target_pct=target,
        target_met=reduction >= target,
    )


def _cost(result: DispatchResult) -> CostBreakdown:
    return CostBreakdown(
        fuel_won=result.fuel_cost_krw,
        startup_won=result.startup_cost_krw,
        recourse_won=0.0,  # 결정론적 MILP 에는 2단계가 없다
        shed_won=result.shed_cost_krw,
        total_won=result.total_cost_krw,
    )


# 급전값이 유효한 solver 상태. 이 밖에서는 제약을 검증할 대상이 없다.
SOLVED_STATUSES = ("optimal", "feasible", "time_limit")


def _checks(
    result: DispatchResult, fleet: list[Generator], inp: DispatchInput
) -> list[CheckItem]:
    """제약 재검증 결과. 해가 없으면 검증하지 않는다.

    infeasible 해는 급전값이 전부 0 이라 "수급 균형 위반"이 뜨는데, 그건 제약을
    어긴 계획이 아니라 계획이 없는 것이다. 그대로 내보내면 원인을 오해한다.
    """
    if not result.has_solution or result.status not in SOLVED_STATUSES:
        return [CheckItem(
            name="제약 검증",
            ok=False,
            detail=f"solver status '{result.status}' — 검증할 급전 계획이 없다",
        )]
    return [
        CheckItem(name=c.name, ok=c.ok, detail=c.detail)
        for c in validation.report(result, fleet, inp.p_init_mw)
    ]


def run_milp(req: MilpRequest) -> MilpResponse:
    """`POST /api/v1/dispatch/milp` 본체.

    status 가 optimal 이 아니면 급전값 대신 진단용 notes 를 채워 돌려준다.
    성공 응답을 억지로 만들지 않는다 (CLAUDE.local.md infeasible 처리).
    """
    if req.carbon_cap_ton is not None and req.allow_load_shedding:
        raise DataError("탄소 상한과 부하차단을 함께 사용할 수 없습니다")
    cfg = settings()
    fleet = generators(cfg)
    if req.carbon_accounting == "lifecycle":
        analysis_config = carbon.load_analysis_config(CONFIG_DIR / "carbon.yaml")
        fleet = carbon.analysis_fleet(fleet, analysis_config)
    inp = build_input(req, cfg, fleet)

    penalty = (
        cfg.shed_penalty_krw_per_mwh if req.shed_penalty_won_per_mwh is None
        else req.shed_penalty_won_per_mwh
    )
    ess = ess_spec(req)
    result = milp.solve(
        inp,
        solver_config=_solver_config(req, cfg),
        hydro_policy=cfg.hydro_policy,
        allow_load_shedding=req.allow_load_shedding,
        shed_penalty_krw_per_mwh=penalty,
        ess=ess,
        carbon_cap_ton=req.carbon_cap_ton,
    )

    comparison: BaselineComparison | None = None
    if req.include_baseline:
        # baseline 은 페널티 없이 돈다. 규칙으로 못 채운 양은 shed_mw 로 남기고
        # 비용에는 섞지 않는다 (섞으면 절감률이 페널티 크기에 좌우된다).
        rule = baseline.run(inp, hydro_policy=cfg.hydro_policy, shed_penalty_krw_per_mwh=0.0)
        reduction = cost_reduction_pct(rule, result) if result.status == "optimal" else 0.0
        if ess is not None:
            result.notes.append(
                "절감률은 'MILP + ESS' vs 'ESS 없는 Rule-based' 비교다. baseline 에 "
                "ESS 를 포함할지는 [미정] (CLAUDE.md) — 팀 확정 전까지 이 조건을 함께 읽을 것"
            )
        comparison = BaselineComparison(
            method=rule.method,
            rules=list(BASELINE_RULES),
            total_cost_won=rule.total_cost_krw,
            fuel_won=rule.fuel_cost_krw,
            startup_won=rule.startup_cost_krw,
            carbon_total_ton=carbon.total_ton(rule.units),
            peak_mw=max((rule.supply_mw(t) for t in range(rule.horizon_h)), default=0.0),
            cost_reduction_pct=reduction,
            target_met=reduction >= COST_REDUCTION_TARGET_PCT,
            constraints=_checks(rule, list(inp.generators), inp),
        )

    size = result.size
    return MilpResponse(
        status=result.status,
        horizon_h=result.horizon_h,
        timestamps=result.timestamps,
        model_size=ModelSizeOut(
            n_variables=size.n_variables if size else 0,
            n_binaries=size.n_binaries if size else 0,
            n_constraints=size.n_constraints if size else 0,
            build_time_s=size.build_time_s if size else 0.0,
            solve_time_s=size.solve_time_s if size else 0.0,
            total_time_s=size.total_time_s if size else 0.0,
        ),
        cost=_cost(result),
        hours=_hours(result),
        units=_units(result) if req.include_units else None,
        merit_order=[MeritOrderEntry(**row) for row in merit_order(result)],
        carbon=_carbon_summary(result, req),
        ess=_ess(result),
        constraints=_checks(result, list(inp.generators), inp) + _cap_checks(result, req),
        baseline=comparison,
        reserve_ratio=inp.reserve_margin_ratio,
        hydro_factor=result.hydro_factor,
        notes=result.notes,
    )


def _cap_checks(result: DispatchResult, req: MilpRequest) -> list[CheckItem]:
    if req.carbon_cap_ton is None or not result.has_solution:
        return []
    total = carbon.total_ton(result.units)
    tolerance = carbon.load_analysis_config(CONFIG_DIR / "carbon.yaml")["pareto"][
        "emission_tolerance_ton"
    ]
    return [CheckItem(name="탄소 상한", ok=total <= req.carbon_cap_ton + tolerance,
                      detail=f"{total:.6f} <= {req.carbon_cap_ton:.6f} t")]


def _carbon_summary(result: DispatchResult, req: MilpRequest) -> CarbonSummary:
    values = carbon.summary(result)
    slack = None
    binding = None
    if req.carbon_cap_ton is not None and result.has_solution:
        tolerance = carbon.load_analysis_config(CONFIG_DIR / "carbon.yaml")["pareto"][
            "emission_tolerance_ton"
        ]
        slack = req.carbon_cap_ton - values["total_ton"]
        binding = abs(slack) <= tolerance
    lifecycle = req.carbon_accounting == "lifecycle"
    return CarbonSummary(
        **values, cap_ton=req.carbon_cap_ton, cap_slack_ton=slack, cap_binding=binding,
        valid=result.has_solution,
        accounting_basis="dispatchable_lifecycle_midpoint" if lifecycle
        else "configured_generator_factors",
        factor_unit="tCO2eq/MWh" if lifecycle else "tCO2/MWh",
    )


def run_pareto(req: ParetoRequest) -> ParetoResponse:
    """분석 입력을 한 번만 로딩하고 동일 조건에서 모든 점을 계산한다."""
    if req.allow_load_shedding or req.carbon_cap_ton is not None:
        raise DataError("Pareto에서는 부하차단 및 외부 carbon_cap_ton을 허용하지 않습니다")
    if any(not math.isfinite(x) or not 0 <= x <= 100 for x in req.reduction_targets_pct):
        raise DataError("감축 목표는 0~100 사이 유한한 백분율이어야 합니다")
    if req.solver and req.solver.time_limit_s is not None:
        limit = req.solver.time_limit_s
        if not math.isfinite(limit) or limit <= 0:
            raise DataError("solver 시간 제한은 양의 유한 값 또는 null이어야 합니다")
    cfg = settings()
    config = carbon.load_analysis_config(CONFIG_DIR / "carbon.yaml")
    fleet = generators(cfg)
    if req.carbon_accounting == "lifecycle":
        fleet = carbon.analysis_fleet(fleet, config)
    inp = build_input(req, cfg, fleet)
    ess = ess_spec(req)
    if ess and ess.mode == "combined":
        raise DataError("비용·탄소 비교에서는 ESS cost 또는 peak_shaving 모드를 사용하세요")
    if ess and ess.soc_final_min_mwh < ess.soc_init_mwh:
        raise DataError("탄소 비교에서는 종료 SOC 하한이 초기 SOC 이상이어야 합니다")
    options = config["pareto"]
    sweep = pareto.run(
        inp, solver_config=_solver_config(req, cfg), hydro_policy=cfg.hydro_policy, ess=ess,
        points=req.points or options["points"],
        total_time_limit_s=req.total_time_limit_s or options["total_time_limit_s"],
        emission_tolerance_ton=options["emission_tolerance_ton"],
        reduction_targets_pct=req.reduction_targets_pct,
    )
    reference = sweep.reference.result if sweep.reference else None
    ref_carbon = carbon.total_ton(reference.units) if reference and reference.has_solution else None
    ref_cost = reference.operating_cost_krw if reference and reference.has_solution else None

    def point(attempt: pareto.Attempt) -> ParetoPoint:
        result = attempt.result
        checks = _checks(result, list(inp.generators), inp)
        emissions = carbon.total_ton(result.units) if result.has_solution else None
        if attempt.cap_ton is not None and emissions is not None:
            checks.append(CheckItem(
                name="탄소 상한",
                ok=emissions <= attempt.cap_ton + options["emission_tolerance_ton"],
                detail=f"{emissions:.6f} <= {attempt.cap_ton:.6f} t",
            ))
        valid = result.has_solution and all(c.ok for c in checks)
        energy: dict[str, float] = {}
        if valid:
            for unit in result.units:
                energy[unit.fuel] = energy.get(unit.fuel, 0) + unit.energy_mwh
        return ParetoPoint(
            label=attempt.label, status=result.status, valid=valid,
            optimal=valid and result.status == "optimal", cap_ton=attempt.cap_ton,
            carbon_ton=emissions if valid else None,
            operating_cost_krw=result.operating_cost_krw if valid else None,
            reduction_pct=(ref_carbon - emissions) / ref_carbon * 100
            if valid and ref_carbon else None,
            cost_increase_pct=(result.operating_cost_krw / ref_cost - 1) * 100
            if valid and ref_cost else None,
            fuel_energy_mwh=energy,
            operations=[CarbonUnitOperation(
                unit_id=u.unit_id, fuel=u.fuel, energy_mwh=u.energy_mwh,
                on_hours=sum(u.on), startups=u.n_startups, p_mw=u.p_mw,
            ) for u in result.units] if valid else [],
            ess=_ess(result) if valid else None, checks=checks,
            elapsed_s=attempt.elapsed_s, notes=result.notes,
        )

    points = [point(a) for a in sweep.attempts]
    by_label = {p.label: p for p in points}
    ref = by_label.get("cost_reference")
    minimum = by_label.get("minimum_carbon_cost_tiebreak")
    # 최적/근사 여부는 점에 보존한다. 모든 점과 실패 기록은 points에 남긴다.
    candidates = sorted((p for p in points if p.valid),
                        key=lambda p: (p.carbon_ton, p.operating_cost_krw))
    frontier: list[ParetoPoint] = []
    etol, ctol = options["emission_tolerance_ton"], options["cost_tolerance_krw"]
    for p in candidates:
        dominated = any(
            q is not p and q.carbon_ton <= p.carbon_ton + etol
            and q.operating_cost_krw <= p.operating_cost_krw + ctol
            and (q.carbon_ton < p.carbon_ton - etol
                 or q.operating_cost_krw < p.operating_cost_krw - ctol)
            for q in candidates
        )
        duplicate = any(abs(q.carbon_ton - p.carbon_ton) <= etol
                        and abs(q.operating_cost_krw - p.operating_cost_krw) <= ctol
                        for q in frontier)
        if not dominated and not duplicate:
            frontier.append(p)
    state = "complete" if sweep.complete else "partial"
    if ref and ref.status in ("infeasible", "error"):
        state = ref.status
    if any(not p.valid and p.status == "optimal" for p in points):
        state = "error"
    return ParetoResponse(
        state=state, timestamps=inp.timestamps,
        accounting_basis=config["basis"] if req.carbon_accounting == "lifecycle"
        else "configured_generator_factors",
        factor_unit=config["unit"] if req.carbon_accounting == "lifecycle" else "tCO2/MWh",
        factors={g.fuel: g.co2_ton_per_mwh for g in inp.generators},
        reference=ref, minimum=minimum,
        maximum_reduction_pct=(ref_carbon - sweep.minimum_emissions_ton) / ref_carbon * 100
        if ref and ref.optimal and ref_carbon and sweep.minimum_emissions_ton is not None
        and by_label["minimum_carbon"].valid else None,
        points=points, frontier=frontier, elapsed_s=sweep.elapsed_s,
        notes=sweep.notes + [
            "동일 수요·예비율·ESS 조건; 부하차단 금지. 기준은 Rule-based가 아닌 비용 최소 MILP.",
            "급전 가능 발전원만의 모델 배출 추정치. 고정 재생발전·별도 기동 배출 제외.",
            "전과정 범위 중간값은 시나리오 가정이며 국내 발전소 실측·공식 감축 실적이 아님."
            if req.carbon_accounting == "lifecycle" else "기존 계수는 산정 범위가 혼합된 가정값.",
            "무연탄은 전용 계수 자료가 없어 석탄 범위 중간값을 대용한 가정임.",
            "frontier는 계산한 점 중 비지배 표본이며 전체 Pareto 집합이 아님.",
        ],
    )
