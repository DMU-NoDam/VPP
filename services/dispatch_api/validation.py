"""급전 결과의 제약 위반 검사.

최적화 코어에서 "비용이 그럴듯하다"는 신뢰할 수 없는 신호다. 그래서 해를 받은
뒤 제약을 **독립적으로 다시 검사**한다 (CLAUDE.md: 결과값뿐 아니라 제약 위반
여부를 테스트한다). MILP 와 baseline 모두 같은 검사를 통과해야 비교가 공정하다.

solver 의 수치오차를 고려해 허용오차(tol)를 둔다. 기본값 1 MW 는 7만 MW 급
수급에서 상대오차 1.4e-5 수준이고, 모델링 실수는 이보다 훨씬 크게 틀린다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from models import DispatchResult, Generator
from schedule import shutdown_ramp_mw, startup_ramp_mw

DEFAULT_TOL_MW = 1.0


@dataclass(frozen=True)
class Violation:
    """제약 위반 1건."""

    constraint: str
    detail: str
    unit_id: str | None = None
    hour: int | None = None

    def __str__(self) -> str:
        where = f"[{self.unit_id or '-'} t={self.hour if self.hour is not None else '-'}]"
        return f"{self.constraint} {where} {self.detail}"


@dataclass(frozen=True)
class CheckResult:
    """제약 1종의 검사 결과. API 의 constraints 필드와 모양이 같다."""

    name: str
    ok: bool
    detail: str


def check_power_balance(
    result: DispatchResult, tol_mw: float = DEFAULT_TOL_MW
) -> list[Violation]:
    """수급 균형: Σ p + ESS 순출력 + 부하차단 - 과잉발전 = 순수요.

    ESS 순출력은 방전(+)과 충전(-)의 합이다. 과잉발전은 baseline 이 램프 한계
    때문에 못 내린 양이다 (MILP 는 항상 0). 균형식에서 빼 주고, 양 자체는 아래
    `report` 가 따로 드러낸다.
    """
    out = []
    for t in range(result.horizon_h):
        gap = (
            result.supply_mw(t) + result.ess_net_mw(t) + result.shed_mw[t]
            - result.surplus_at(t) - result.net_demand_mw[t]
        )
        if abs(gap) > tol_mw:
            out.append(Violation(
                "power_balance", f"공급-수요 {gap:+.2f} MW", hour=t,
            ))
    return out


def check_capacity(
    result: DispatchResult, generators: Sequence[Generator], tol_mw: float = DEFAULT_TOL_MW
) -> list[Violation]:
    """출력 상·하한: p_min*u <= p <= p_max_eff*u (정지 시 p = 0)."""
    by_id = {g.id: g for g in generators}
    out = []
    for unit in result.units:
        g = by_id[unit.unit_id]
        for t, (on, p) in enumerate(zip(unit.on, unit.p_mw, strict=True)):
            if not on:
                if abs(p) > tol_mw:
                    out.append(Violation(
                        "capacity", f"정지 상태인데 출력 {p:.2f} MW",
                        unit_id=g.id, hour=t,
                    ))
                continue
            if p > unit.p_max_eff_mw + tol_mw:
                out.append(Violation(
                    "capacity", f"p {p:.2f} > p_max {unit.p_max_eff_mw:.2f} MW",
                    unit_id=g.id, hour=t,
                ))
            if p < g.p_min_mw - tol_mw:
                out.append(Violation(
                    "capacity", f"p {p:.2f} < p_min {g.p_min_mw:.2f} MW",
                    unit_id=g.id, hour=t,
                ))
    return out


def check_reserve(
    result: DispatchResult, tol_mw: float = DEFAULT_TOL_MW
) -> list[Violation]:
    """예비율: 가동 중 유닛의 상한 합 >= (1 + r) x 발전기 부하.

    발전기 부하 = 공급한 수요 + ESS 충전 - ESS 방전 (ESS 가 없으면 수요와 같다).
    """
    out = []
    required_ratio = 1.0 + result.reserve_margin_ratio
    for t in range(result.horizon_h):
        required = required_ratio * result.generator_load_mw(t)
        available = result.available_capacity_mw(t)
        if available < required - tol_mw:
            out.append(Violation(
                "reserve",
                f"가동용량 {available:,.0f} MW < 필요 {required:,.0f} MW "
                f"(예비율 {result.reserve_pct(t):.1f}%)",
                hour=t,
            ))
    return out


def check_ramp(
    result: DispatchResult,
    generators: Sequence[Generator],
    p_init_mw: dict[str, float] | None = None,
    tol_mw: float = DEFAULT_TOL_MW,
) -> list[Violation]:
    """램프율. 기동·정지 시점은 startup/shutdown 램프 한계를 쓴다.

    p_init_mw 를 모르면 t=0 은 검사하지 않는다 (MILP 도 제약을 걸지 않는다).
    """
    by_id = {g.id: g for g in generators}
    out = []
    for unit in result.units:
        g = by_id[unit.unit_id]
        su = startup_ramp_mw(g, unit.p_max_eff_mw)
        sd = shutdown_ramp_mw(g, unit.p_max_eff_mw)
        for t in range(result.horizon_h):
            if t == 0:
                if p_init_mw is None or g.id not in p_init_mw:
                    continue
                prev_p = p_init_mw[g.id]
                prev_on = bool(g.u_init)
            else:
                prev_p = unit.p_mw[t - 1]
                prev_on = unit.on[t - 1]

            up_limit = g.ramp_up_mw_per_h * prev_on + su * unit.startup[t]
            if unit.p_mw[t] - prev_p > up_limit + tol_mw:
                out.append(Violation(
                    "ramp_up",
                    f"증가 {unit.p_mw[t] - prev_p:.2f} MW > 한계 {up_limit:.2f} MW",
                    unit_id=g.id, hour=t,
                ))
            down_limit = g.ramp_down_mw_per_h * unit.on[t] + sd * unit.shutdown[t]
            if prev_p - unit.p_mw[t] > down_limit + tol_mw:
                out.append(Violation(
                    "ramp_down",
                    f"감소 {prev_p - unit.p_mw[t]:.2f} MW > 한계 {down_limit:.2f} MW",
                    unit_id=g.id, hour=t,
                ))
    return out


def _runs(on: Sequence[bool], u_init: int, init_state_h: int) -> list[tuple[bool, int, int, bool]]:
    """(상태, 시작시각, 지속시간, 끝이 horizon 밖인지) 목록.

    첫 구간의 지속시간에는 초기 상태가 이어진 시간(init_state_h)을 더한다.
    마지막 구간은 horizon 에서 잘렸을 뿐이라 최소시간 위반으로 보지 않는다.
    """
    runs = []
    state = bool(on[0])
    start = 0
    carry = init_state_h if state == bool(u_init) else 0
    for t in range(1, len(on)):
        if bool(on[t]) != state:
            runs.append((state, start, t - start + carry, False))
            state, start, carry = bool(on[t]), t, 0
    runs.append((state, start, len(on) - start + carry, True))
    return runs


def check_min_up_down(
    result: DispatchResult, generators: Sequence[Generator]
) -> list[Violation]:
    """최소 기동/정지 시간. 초기 상태가 이어진 시간을 포함해서 센다."""
    by_id = {g.id: g for g in generators}
    out = []
    for unit in result.units:
        g = by_id[unit.unit_id]
        for state, start, length, truncated in _runs(unit.on, g.u_init, g.init_state_h):
            if truncated:
                continue
            limit = g.min_up_h if state else g.min_down_h
            if length < limit:
                kind = "min_up" if state else "min_down"
                out.append(Violation(
                    kind,
                    f"{'가동' if state else '정지'} {length}h < 최소 {limit}h",
                    unit_id=g.id, hour=start,
                ))
    return out


def check_hydro_limit(
    result: DispatchResult, generators: Sequence[Generator], tol_mw: float = DEFAULT_TOL_MW
) -> list[Violation]:
    """수력 저수율 제한이 상한에 반영됐는지. 출력 자체는 check_capacity 가 본다."""
    by_id = {g.id: g for g in generators}
    out = []
    for unit in result.units:
        g = by_id[unit.unit_id]
        expected = g.p_max_mw * (result.hydro_factor if g.fuel == "hydro" else 1.0)
        if abs(unit.p_max_eff_mw - expected) > tol_mw:
            out.append(Violation(
                "hydro_limit",
                f"상한 {unit.p_max_eff_mw:.2f} MW != 기대 {expected:.2f} MW "
                f"(factor {result.hydro_factor:.3f})",
                unit_id=g.id,
            ))
    return out


def check_ess(
    result: DispatchResult, tol_mw: float = DEFAULT_TOL_MW
) -> list[Violation]:
    """ESS 출력 한계·SOC 범위·SOC 전이식·종료 SOC·동시 충방전.

    SOC 전이식을 **다시 계산해서** 비교한다. 효율을 빼먹거나 충·방전 부호를
    바꿔 쓰는 실수는 비용이 그럴듯하게 나오기 때문에 결과만 보면 안 잡힌다.
    """
    ess = result.ess
    if ess is None:
        return []

    spec = ess.spec
    out: list[Violation] = []
    tol_mwh = tol_mw  # step 1시간이라 MWh 허용오차도 같은 값을 쓴다

    for t in range(result.horizon_h):
        if ess.charge_mw[t] > spec.power_charge_mw + tol_mw:
            out.append(Violation(
                "ess_power",
                f"충전 {ess.charge_mw[t]:.2f} > 한계 {spec.power_charge_mw:.2f} MW",
                unit_id="ess", hour=t,
            ))
        if ess.discharge_mw[t] > spec.power_discharge_mw + tol_mw:
            out.append(Violation(
                "ess_power",
                f"방전 {ess.discharge_mw[t]:.2f} > 한계 {spec.power_discharge_mw:.2f} MW",
                unit_id="ess", hour=t,
            ))
        if ess.charge_mw[t] < -tol_mw or ess.discharge_mw[t] < -tol_mw:
            out.append(Violation(
                "ess_power", "충전·방전은 음수일 수 없다", unit_id="ess", hour=t,
            ))

        if not (spec.soc_min_mwh - tol_mwh <= ess.soc_mwh[t] <= spec.soc_max_mwh + tol_mwh):
            out.append(Violation(
                "ess_soc_bounds",
                f"SOC {ess.soc_mwh[t]:,.1f} MWh 가 "
                f"[{spec.soc_min_mwh:,.1f}, {spec.soc_max_mwh:,.1f}] 밖이다",
                unit_id="ess", hour=t,
            ))

        previous = ess.soc_mwh[t - 1] if t > 0 else spec.soc_init_mwh
        expected = (
            previous
            + spec.eta_charge * ess.charge_mw[t]
            - ess.discharge_mw[t] / spec.eta_discharge
        )
        if abs(ess.soc_mwh[t] - expected) > tol_mwh:
            out.append(Violation(
                "ess_soc_balance",
                f"SOC {ess.soc_mwh[t]:,.2f} != 전이식 결과 {expected:,.2f} MWh "
                f"(차이 {ess.soc_mwh[t] - expected:+.2f})",
                unit_id="ess", hour=t,
            ))

        if (
            spec.enforce_exclusive
            and ess.charge_mw[t] > tol_mw
            and ess.discharge_mw[t] > tol_mw
        ):
            out.append(Violation(
                "ess_exclusive",
                f"동시 충전 {ess.charge_mw[t]:.1f} / 방전 {ess.discharge_mw[t]:.1f} MW",
                unit_id="ess", hour=t,
            ))

    if ess.soc_mwh and ess.soc_mwh[-1] < spec.soc_final_min_mwh - tol_mwh:
        out.append(Violation(
            "ess_soc_final",
            f"종료 SOC {ess.soc_mwh[-1]:,.1f} < 하한 {spec.soc_final_min_mwh:,.1f} MWh "
            "(배터리를 비우고 끝내면 비용이 부당하게 낮아진다)",
            unit_id="ess", hour=result.horizon_h - 1,
        ))
    return out


def all_violations(
    result: DispatchResult,
    generators: Sequence[Generator],
    p_init_mw: dict[str, float] | None = None,
    tol_mw: float = DEFAULT_TOL_MW,
) -> list[Violation]:
    """필수 제약 전체 검사. 비어 있으면 그 해는 모든 제약을 만족한다."""
    return [
        *check_power_balance(result, tol_mw),
        *check_capacity(result, generators, tol_mw),
        *check_reserve(result, tol_mw),
        *check_ramp(result, generators, p_init_mw, tol_mw),
        *check_min_up_down(result, generators),
        *check_hydro_limit(result, generators, tol_mw),
        *check_ess(result, tol_mw),
    ]


# 체크 이름 -> API/리포트에 쓸 한국어 라벨. 필수 제약 체크리스트 순서다.
CHECK_LABELS: tuple[tuple[str, str], ...] = (
    ("power_balance", "수급 균형"),
    ("capacity", "출력 상·하한"),
    ("reserve", "예비율"),
    ("ramp_up", "램프업"),
    ("ramp_down", "램프다운"),
    ("min_up", "최소 기동시간"),
    ("min_down", "최소 정지시간"),
    ("hydro_limit", "수력 저수율 제한"),
    ("ess_power", "ESS 출력 한계"),
    ("ess_soc_bounds", "ESS SOC 범위"),
    ("ess_soc_balance", "ESS SOC 전이식"),
    ("ess_soc_final", "ESS 종료 SOC"),
    ("ess_exclusive", "ESS 동시 충방전 금지"),
)


def report(
    result: DispatchResult,
    generators: Sequence[Generator],
    p_init_mw: dict[str, float] | None = None,
    tol_mw: float = DEFAULT_TOL_MW,
) -> list[CheckResult]:
    """제약별 통과 여부 요약. 위반이 있으면 첫 건을 detail 에 넣는다."""
    violations = all_violations(result, generators, p_init_mw, tol_mw)
    grouped: dict[str, list[Violation]] = {}
    for v in violations:
        grouped.setdefault(v.constraint, []).append(v)

    checks = [
        CheckResult(
            name=label,
            ok=key not in grouped,
            detail=(
                "위반 없음" if key not in grouped
                else f"{len(grouped[key])}건, 예: {grouped[key][0]}"
            ),
        )
        for key, label in CHECK_LABELS
        # ESS 를 쓰지 않은 해에 ESS 항목을 "통과"로 보여주면 검증한 것처럼 오해된다
        if result.ess is not None or not key.startswith("ess_")
    ]

    surplus_mwh = sum(result.surplus_mw)
    checks.append(CheckResult(
        name="과잉발전 없음",
        ok=surplus_mwh <= tol_mw,
        detail=(
            "없음" if surplus_mwh <= tol_mw
            else f"{surplus_mwh:,.1f} MWh 과잉 (램프 한계로 감발 불가)"
        ),
    ))

    shed_mwh = sum(result.shed_mw)
    checks.append(CheckResult(
        name="부하차단 미사용",
        ok=shed_mwh <= tol_mw,
        detail="없음" if shed_mwh <= tol_mw else f"{shed_mwh:,.1f} MWh 차단 (용량 부족)",
    ))
    return checks
