"""탄소 배출 계산 (docs/formulation.md 6장).

급전 스케줄을 입력으로 받는 독립 모듈이다. MILP 도 baseline 도 ESS 전/후도
같은 함수를 쓴다 (CLAUDE.local.md: 후처리가 아니라 독립 모듈).

    배출량(tCO2) = 발전량(MWh) x 배출계수(tCO2/MWh)

time step 이 1시간이므로 출력(MW) 합이 곧 발전량(MWh)이다. step 을 바꾸면
이 가정이 깨지므로 `step_h` 로 노출한다.
"""

from __future__ import annotations

from collections.abc import Sequence

from models import DispatchResult, UnitSchedule


def hourly_ton(units: Sequence[UnitSchedule], step_h: float = 1.0) -> list[float]:
    """시간대별 배출량 (tCO2)."""
    if not units:
        return []
    horizon = len(units[0].p_mw)
    return [
        sum(u.co2_ton_per_mwh * u.p_mw[t] * step_h for u in units)
        for t in range(horizon)
    ]


def total_ton(units: Sequence[UnitSchedule], step_h: float = 1.0) -> float:
    """기간 총배출량 (tCO2)."""
    return sum(hourly_ton(units, step_h))


def by_fuel_ton(units: Sequence[UnitSchedule], step_h: float = 1.0) -> dict[str, float]:
    """발전원별 총배출량 (tCO2)."""
    out: dict[str, float] = {}
    for u in units:
        out[u.fuel] = out.get(u.fuel, 0.0) + u.co2_ton_per_mwh * u.energy_mwh * step_h
    return out


def by_unit_ton(units: Sequence[UnitSchedule], step_h: float = 1.0) -> dict[str, float]:
    """유닛별 총배출량 (tCO2)."""
    return {u.unit_id: u.co2_ton_per_mwh * u.energy_mwh * step_h for u in units}


def intensity_ton_per_mwh(units: Sequence[UnitSchedule]) -> float:
    """평균 배출집약도 (tCO2/MWh). 급전 전략 비교에 쓴다."""
    energy = sum(u.energy_mwh for u in units)
    return total_ton(units) / energy if energy > 0 else 0.0


def summary(result: DispatchResult, step_h: float = 1.0) -> dict[str, object]:
    """API·대시보드용 요약. 비용-탄소 Pareto Front 확장 시에도 이 모양을 유지한다."""
    return {
        "total_ton": total_ton(result.units, step_h),
        "hourly_ton": hourly_ton(result.units, step_h),
        "by_fuel_ton": by_fuel_ton(result.units, step_h),
        "intensity_ton_per_mwh": intensity_ton_per_mwh(result.units),
    }


def compare(before: DispatchResult, after: DispatchResult, step_h: float = 1.0) -> dict[str, float]:
    """두 스케줄의 배출량 비교 (ESS 전/후, baseline vs MILP 등).

    reduction_pct 는 before 기준 감소율이다. before 가 0 이면 0 을 돌려준다.
    """
    b = total_ton(before.units, step_h)
    a = total_ton(after.units, step_h)
    return {
        "before_ton": b,
        "after_ton": a,
        "delta_ton": a - b,
        "reduction_pct": (b - a) / b * 100.0 if b > 0 else 0.0,
    }
