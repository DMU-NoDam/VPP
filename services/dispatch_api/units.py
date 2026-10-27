"""발전기 제원 로더 — config/generators.yaml (C 의 v1 형식, 25기) → Unit 목록.

stochastic.py 는 이 모듈의 Unit 필드만 쓴다. C 의 MILP 로더가 생기면 load_units 를
그쪽 로더로 바꾸고, 필드 이름만 맞춰 주면 stochastic.py 는 그대로 동작한다.

연료비는 generators.yaml 에 없다 (월별로 변하는 값). collector 의 fuel_cost 데이터셋
(month, fuel, cost_won_per_kwh)에서 최신 월 값을 REST 로 받아 fuel 코드로 조인하고,
원/kWh → 원/MWh 변환(constants.KRW_PER_KWH_TO_KRW_PER_MWH)은 여기 한 곳에서만 한다.
collector 가 응답하지 않으면 data/csv/fuel_cost.csv 스냅샷을 읽는다. 유닛에
fuel_cost_krw_per_mwh 가 적혀 있으면 (수력) 조인 대신 그 값을 쓴다.

D 쪽 모듈(stochastic, precompute, scenarios)이 config/stochastic.yaml 을 읽는 도우미
(read_stochastic_yaml, cfg_value)도 여기 둔다.
"""

from __future__ import annotations

import csv
import json
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

import constants

HERE = Path(__file__).resolve()


@dataclass(frozen=True)
class Unit:
    id: str
    fuel: str
    p_max_mw: float
    p_min_mw: float
    ramp_up_mw_per_h: float
    ramp_down_mw_per_h: float
    min_up_h: int
    min_down_h: int
    startup_cost_krw: float
    co2_ton_per_mwh: float
    u_init: int
    init_state_h: int
    fuel_cost_krw_per_mwh: float


def find_file(env: str, *relative: str) -> Path:
    """환경변수 → /app/... (컨테이너) → 레포 루트 순으로 찾는다."""
    if os.getenv(env):
        return Path(os.environ[env])
    for root in (Path("/app"), *HERE.parents):
        candidate = root.joinpath(*relative)
        if candidate.exists():
            return candidate
    raise FileNotFoundError("/".join(relative))


def read_stochastic_yaml(path: Path | None = None) -> dict[str, Any]:
    """config/stochastic.yaml 전체."""
    path = path or find_file("STOCHASTIC_YAML", "config", "stochastic.yaml")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def cfg_value(node: dict[str, Any], unit: str) -> Any:
    """{value, unit, source} 항목의 value. unit 이 기대와 다르면 멈춘다 (변환하지 않는다)."""
    if not isinstance(node, dict) or "value" not in node:
        raise ValueError(f"config 값은 {{value, unit, source}} 형식이어야 한다: {node!r}")
    if node.get("unit") != unit:
        raise ValueError(f"unit 이 '{unit}' 이어야 하는데 '{node.get('unit')}' 이다: {node!r}")
    return node["value"]


def latest_fuel_costs(rows: list[dict]) -> dict[str, float]:
    """fuel_cost 행 목록에서 연료별 최신 월 단가(원/MWh)."""
    latest: dict[str, tuple[str, float]] = {}
    for row in rows:
        fuel, month = row["fuel"], str(row["month"])
        if row.get("cost_won_per_kwh") in (None, ""):
            continue
        if fuel not in latest or month > latest[fuel][0]:
            cost = float(row["cost_won_per_kwh"]) * constants.KRW_PER_KWH_TO_KRW_PER_MWH
            latest[fuel] = (month, cost)
    return {fuel: cost for fuel, (_, cost) in latest.items()}


def fetch_fuel_costs(timeout_s: float | None = None) -> dict[str, float]:
    """collector REST → 실패 시 CSV 스냅샷. timeout_s·조회 행 수는 config 의 collector 블록."""
    cfg = read_stochastic_yaml()["collector"]
    if timeout_s is None:
        timeout_s = float(cfg_value(cfg["fuel_cost_timeout"], "s"))
    rows = int(cfg_value(cfg["fuel_cost_rows"], "count"))
    url = os.getenv("COLLECTOR_URL", "http://collector:8000") + f"/data/fuel_cost?limit={rows}"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310 (내부 URL)
            return latest_fuel_costs(json.load(resp)["rows"])
    except (OSError, ValueError, KeyError):
        path = find_file("FUEL_COST_CSV", "data", "csv", "fuel_cost.csv")
        with path.open(encoding="utf-8") as f:
            return latest_fuel_costs(list(csv.DictReader(f)))


def load_units(
    path: Path | None = None,
    fuel_costs: dict[str, float] | None = None,
    ids: list[str] | None = None,
) -> list[Unit]:
    """generators.yaml 을 읽어 Unit 목록을 만든다. ids 를 주면 그 유닛만."""
    path = path or find_file("GENERATORS_YAML", "config", "generators.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))["generators"]
    costs = fuel_costs if fuel_costs is not None else fetch_fuel_costs()

    units = []
    for g in raw:
        if ids is not None and g["id"] not in ids:
            continue
        if "fuel_cost_krw_per_mwh" in g:
            cost = float(g["fuel_cost_krw_per_mwh"])
        elif g["fuel"] in costs:
            cost = costs[g["fuel"]]
        else:
            raise KeyError(f"{g['id']}: fuel_cost 에 '{g['fuel']}' 단가가 없다")
        units.append(
            Unit(
                id=g["id"],
                fuel=g["fuel"],
                p_max_mw=float(g["p_max_mw"]),
                p_min_mw=float(g["p_min_mw"]),
                ramp_up_mw_per_h=float(g["ramp_up_mw_per_h"]),
                ramp_down_mw_per_h=float(g["ramp_down_mw_per_h"]),
                min_up_h=int(g["min_up_h"]),
                min_down_h=int(g["min_down_h"]),
                startup_cost_krw=float(g["startup_cost_krw"]),
                co2_ton_per_mwh=float(g["co2_ton_per_mwh"]),
                u_init=int(g["u_init"]),
                init_state_h=int(g["init_state_h"]),
                fuel_cost_krw_per_mwh=cost,
            )
        )
    if not units:
        raise ValueError("선택된 발전기가 없다")
    return units
