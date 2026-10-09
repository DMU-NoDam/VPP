"""config/*.yaml -> 타입 있는 도메인 객체.

책임
  1. yaml 을 읽어 dataclass 로 바꾼다
  2. 불변식을 검증한다 (어긋나면 어느 유닛 어느 필드인지 말하며 예외)
  3. 연료비를 결합하고 원/kWh -> 원/MWh 변환을 **이 모듈 한 곳에서만** 한다

전부 순수 함수다. 네트워크도 전역 상태도 쓰지 않는다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

import constants
from models import Generator, GeneratorSpec, Settings

logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """config 파일이 규칙을 어겼을 때. 메시지에 위치와 이유를 담는다."""


# generators.yaml 유닛 하나에 반드시 있어야 하는 필드.
REQUIRED_GENERATOR_FIELDS: tuple[str, ...] = (
    "id",
    "fuel",
    "p_max_mw",
    "p_min_mw",
    "ramp_up_mw_per_h",
    "ramp_down_mw_per_h",
    "min_up_h",
    "min_down_h",
    "startup_cost_krw",
    "co2_ton_per_mwh",
    "u_init",
    "init_state_h",
)


def load_yaml(path: str | Path) -> dict[str, Any]:
    """yaml 파일 하나를 dict 로. 비어 있거나 매핑이 아니면 예외."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config 파일이 없다: {path}")

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: 최상위가 매핑이 아니다 (읽은 타입: {type(data).__name__})")
    return data


# ── 발전기 제원 ──────────────────────────────────────────────────────────────


def load_generator_specs(path: str | Path) -> list[GeneratorSpec]:
    """generators.yaml -> GeneratorSpec 목록. 연료비는 아직 결합하지 않는다."""
    data = load_yaml(path)

    raw = data.get("generators")
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{path}: `generators` 가 비어 있거나 리스트가 아니다")

    specs = [_parse_generator(entry, index, path) for index, entry in enumerate(raw)]
    _check_unique_ids(specs, path)
    return specs


def _parse_generator(entry: Any, index: int, path: str | Path) -> GeneratorSpec:
    """유닛 하나를 파싱하고 불변식을 검증한다."""
    where = f"{path}: generators[{index}]"
    if not isinstance(entry, dict):
        raise ConfigError(f"{where}: 매핑이 아니다")

    missing = [f for f in REQUIRED_GENERATOR_FIELDS if f not in entry]
    if missing:
        gen_id = entry.get("id", "<id 없음>")
        raise ConfigError(f"{where} ({gen_id}): 필수 필드 누락 {missing}")

    gen_id = str(entry["id"])
    where = f"{path}: {gen_id}"

    fuel = str(entry["fuel"])
    if fuel not in constants.FUEL_CODES:
        raise ConfigError(
            f"{where}: fuel '{fuel}' 은 연료 코드 9종에 없다. "
            f"허용: {list(constants.FUEL_CODES)}"
        )

    spec = GeneratorSpec(
        id=gen_id,
        fuel=fuel,
        p_max_mw=_number(entry, "p_max_mw", where),
        p_min_mw=_number(entry, "p_min_mw", where),
        ramp_up_mw_per_h=_number(entry, "ramp_up_mw_per_h", where),
        ramp_down_mw_per_h=_number(entry, "ramp_down_mw_per_h", where),
        min_up_h=_integer(entry, "min_up_h", where),
        min_down_h=_integer(entry, "min_down_h", where),
        startup_cost_krw=_number(entry, "startup_cost_krw", where),
        co2_ton_per_mwh=_number(entry, "co2_ton_per_mwh", where),
        u_init=_integer(entry, "u_init", where),
        init_state_h=_integer(entry, "init_state_h", where),
        fuel_cost_krw_per_mwh=(
            _number(entry, "fuel_cost_krw_per_mwh", where)
            if "fuel_cost_krw_per_mwh" in entry
            else None
        ),
    )
    _check_generator_invariants(spec, where)
    return spec


def _check_generator_invariants(spec: GeneratorSpec, where: str) -> None:
    """물리적으로 불가능한 조합을 걸러낸다.

    여기서 못 잡으면 solver 가 infeasible 을 내고, 원인을 로그에서 찾느라 시간을
    쓴다. 그래서 가능한 한 많이 여기서 잡는다.
    """
    if spec.p_max_mw <= 0:
        raise ConfigError(f"{where}: p_max_mw 는 양수여야 한다 (받은 값 {spec.p_max_mw})")
    if spec.p_min_mw < 0:
        raise ConfigError(f"{where}: p_min_mw 는 음수일 수 없다 (받은 값 {spec.p_min_mw})")
    if spec.p_min_mw > spec.p_max_mw:
        raise ConfigError(
            f"{where}: p_min_mw({spec.p_min_mw}) > p_max_mw({spec.p_max_mw}) 다. "
            "가동 시 출력 구간이 비어 있어 infeasible 이 된다"
        )
    if spec.ramp_up_mw_per_h <= 0 or spec.ramp_down_mw_per_h <= 0:
        raise ConfigError(
            f"{where}: ramp 는 양수여야 한다 "
            f"(up {spec.ramp_up_mw_per_h}, down {spec.ramp_down_mw_per_h})"
        )
    if spec.min_up_h < 1 or spec.min_down_h < 1:
        raise ConfigError(
            f"{where}: min_up_h / min_down_h 는 1 이상이어야 한다 "
            f"(up {spec.min_up_h}, down {spec.min_down_h})"
        )
    if spec.startup_cost_krw < 0:
        raise ConfigError(f"{where}: startup_cost_krw 는 음수일 수 없다")
    if spec.co2_ton_per_mwh < 0:
        raise ConfigError(f"{where}: co2_ton_per_mwh 는 음수일 수 없다")
    if spec.u_init not in (0, 1):
        raise ConfigError(f"{where}: u_init 은 0 또는 1 이어야 한다 (받은 값 {spec.u_init})")
    if spec.init_state_h < 0:
        raise ConfigError(f"{where}: init_state_h 는 음수일 수 없다")
    if spec.fuel_cost_krw_per_mwh is not None and spec.fuel_cost_krw_per_mwh < 0:
        raise ConfigError(f"{where}: fuel_cost_krw_per_mwh 는 음수일 수 없다")


def _check_unique_ids(specs: list[GeneratorSpec], path: str | Path) -> None:
    seen: set[str] = set()
    for spec in specs:
        if spec.id in seen:
            raise ConfigError(f"{path}: id '{spec.id}' 가 중복이다")
        seen.add(spec.id)


def _number(entry: dict[str, Any], key: str, where: str) -> float:
    value = entry[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: {key} 가 숫자가 아니다 (받은 값 {value!r})")
    return float(value)


def _integer(entry: dict[str, Any], key: str, where: str) -> int:
    value = entry[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: {key} 는 정수여야 한다 (받은 값 {value!r})")
    return value


# ── 연료비 결합 ──────────────────────────────────────────────────────────────


def select_fuel_cost_month(rows: list[dict[str, Any]], requested: str = "latest") -> str:
    """사용할 월을 고른다.

    `requested` 가 "latest" 면 데이터에 있는 가장 최근 월. 특정 월(YYYY-MM)을 줬는데
    데이터에 없으면 최신 월로 fallback 하고 경고를 남긴다 — 조용히 넘어가면 어느
    달 단가로 계산했는지 모르게 된다.
    """
    months = sorted({str(r["month"]) for r in rows if r.get("month") is not None})
    if not months:
        raise ConfigError("fuel_cost 데이터에 month 가 없다")

    if requested == "latest":
        return months[-1]

    if requested in months:
        return requested

    logger.warning(
        "fuel_cost 에 %s 가 없다 — 최신 월 %s 로 대체한다 (가용: %s)",
        requested, months[-1], months,
    )
    return months[-1]


def resolve_fuel_costs(
    specs: list[GeneratorSpec],
    fuel_cost_rows: list[dict[str, Any]],
    requested_month: str = "latest",
) -> list[Generator]:
    """GeneratorSpec + fuel_cost 데이터 -> Generator.

    원/kWh -> 원/MWh 변환이 일어나는 **유일한 지점**이다. 다른 어디에서도 1000 을
    곱하지 않는다.

    yaml 에 `fuel_cost_krw_per_mwh` override 가 있으면 조인하지 않고 그 값을 쓴다
    (수력처럼 fuel_cost 데이터셋에 항목이 없는 발전원).

    조인에 실패한 연료가 있으면 예외를 낸다. 0 으로 채우면 그 발전기가 공짜가 되어
    merit order 최상위로 올라가고, 결과가 그럴듯해 보이면서 완전히 틀린다.
    """
    month = select_fuel_cost_month(fuel_cost_rows, requested_month)

    cost_krw_per_mwh: dict[str, float] = {}
    for row in fuel_cost_rows:
        if str(row.get("month")) != month:
            continue
        raw = row.get("cost_won_per_kwh")
        if raw is None:
            continue
        cost_krw_per_mwh[str(row["fuel"])] = (
            float(raw) * constants.KRW_PER_KWH_TO_KRW_PER_MWH
        )

    needs_join = {s.fuel for s in specs if s.fuel_cost_krw_per_mwh is None}
    missing = sorted(needs_join - set(cost_krw_per_mwh))
    if missing:
        raise ConfigError(
            f"fuel_cost({month}) 에 없는 연료: {missing}. "
            "generators.yaml 에 fuel_cost_krw_per_mwh 를 명시하거나 데이터를 보강할 것 "
            f"(가용 연료: {sorted(cost_krw_per_mwh)})"
        )

    generators = [
        Generator(
            id=s.id,
            fuel=s.fuel,
            p_max_mw=s.p_max_mw,
            p_min_mw=s.p_min_mw,
            ramp_up_mw_per_h=s.ramp_up_mw_per_h,
            ramp_down_mw_per_h=s.ramp_down_mw_per_h,
            min_up_h=s.min_up_h,
            min_down_h=s.min_down_h,
            startup_cost_krw=s.startup_cost_krw,
            co2_ton_per_mwh=s.co2_ton_per_mwh,
            u_init=s.u_init,
            init_state_h=s.init_state_h,
            fuel_cost_krw_per_mwh=(
                s.fuel_cost_krw_per_mwh
                if s.fuel_cost_krw_per_mwh is not None
                else cost_krw_per_mwh[s.fuel]
            ),
        )
        for s in specs
    ]

    logger.info(
        "연료비 결합 완료 (%s): %d기, 단가 %s",
        month,
        len(generators),
        {f: round(c, 1) for f, c in sorted(cost_krw_per_mwh.items())},
    )
    return generators


# ── 운영 설정 ────────────────────────────────────────────────────────────────

VALID_RESAMPLE = ("mean", "instant")
VALID_NET_DEMAND_MODES = (
    "subtract_solar_renewable",
    "subtract_renewable_only",
    "demand_only",
)


def load_settings(path: str | Path) -> Settings:
    """settings.yaml -> Settings. 선택지가 있는 필드는 값을 검증한다."""
    data = load_yaml(path)
    where = str(path)

    resample = str(data.get("resample", "mean"))
    if resample not in VALID_RESAMPLE:
        raise ConfigError(f"{where}: resample '{resample}' 는 {VALID_RESAMPLE} 중 하나여야 한다")

    net_demand_mode = str(data.get("net_demand_mode", "subtract_solar_renewable"))
    if net_demand_mode not in VALID_NET_DEMAND_MODES:
        raise ConfigError(
            f"{where}: net_demand_mode '{net_demand_mode}' 는 "
            f"{VALID_NET_DEMAND_MODES} 중 하나여야 한다"
        )

    horizon_h = int(data.get("horizon_h", 24))
    if horizon_h < 1:
        raise ConfigError(f"{where}: horizon_h 는 1 이상이어야 한다")

    time_step_min = int(data.get("time_step_min", constants.MINUTES_PER_HOUR))
    if time_step_min != constants.MINUTES_PER_HOUR:
        raise ConfigError(
            f"{where}: time_step_min={time_step_min} — 현재 로더와 MILP 는 1시간 "
            "step 만 지원한다. 바꾸려면 리샘플과 램프/최소시간 제약 단위를 함께 고쳐야 한다"
        )

    reserve = float(data.get("reserve_margin_ratio", 0.10))
    if reserve < 0:
        raise ConfigError(f"{where}: reserve_margin_ratio 는 음수일 수 없다")

    time_limit = data.get("solver_time_limit_s")
    return Settings(
        horizon_h=horizon_h,
        time_step_min=time_step_min,
        timezone=str(data.get("timezone", constants.DEFAULT_TIMEZONE)),
        solver=str(data.get("solver", "HiGHS")),
        solver_msg=bool(data.get("solver_msg", False)),
        solver_time_limit_s=int(time_limit) if time_limit else None,
        solver_mip_gap=float(data.get("solver_mip_gap", 0.0)),
        reserve_margin_ratio=reserve,
        resample=resample,
        net_demand_mode=net_demand_mode,
        fuel_cost_month=str(data.get("fuel_cost_month", "latest")),
    )
