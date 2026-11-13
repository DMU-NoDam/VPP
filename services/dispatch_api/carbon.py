"""탄소 배출 계산 (docs/formulation.md 6장).

급전 스케줄을 입력으로 받는 독립 모듈이다. MILP 도 baseline 도 ESS 전/후도
같은 함수를 쓴다 (CLAUDE.local.md: 후처리가 아니라 독립 모듈).

    배출량(tCO2) = 발전량(MWh) x 배출계수(tCO2/MWh)

time step 이 1시간이므로 출력(MW) 합이 곧 발전량(MWh)이다. step 을 바꾸면
이 가정이 깨지므로 `step_h` 로 노출한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from models import DispatchResult, Generator, UnitSchedule


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


def load_analysis_config(path: str | Path) -> dict[str, Any]:
    """분석 계수와 수치 허용오차. 기본 급전의 계수는 변경하지 않는다."""
    import math

    import yaml

    from loaders.config_loader import ConfigError

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    try:
        factors = raw["factors"]
        options = raw["pareto"]
        numbers = list(factors.values()) + [
            options["total_time_limit_s"], options["emission_tolerance_ton"],
            options["cost_tolerance_krw"],
        ]
        if not factors or any(not math.isfinite(float(x)) or float(x) < 0 for x in numbers):
            raise ValueError("계수·허용오차는 유한한 비음수여야 한다")
        if not 2 <= options["points"] <= 20 or options["total_time_limit_s"] <= 0:
            raise ValueError("점 개수 또는 시간 제한 오류")
        if raw["unit"] != "tCO2eq/MWh" or not raw["basis"] or not raw["source"]:
            raise ValueError("배출 단위·출처·범위 필요")
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"carbon.yaml: {exc}") from exc
    return raw


def analysis_fleet(fleet: Sequence[Generator], config: dict[str, Any]) -> list[Generator]:
    """모든 유닛에 같은 산정 범위의 계수를 적용한다. 누락 시 0으로 대체하지 않는다."""
    from dataclasses import replace

    from loaders.config_loader import ConfigError

    missing = {g.fuel for g in fleet} - config["factors"].keys()
    if missing:
        raise ConfigError(f"탄소 분석 계수가 없는 발전원: {sorted(missing)}")
    return [replace(g, co2_ton_per_mwh=float(config["factors"][g.fuel])) for g in fleet]
