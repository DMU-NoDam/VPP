"""대시보드 로직 — 설정 로드, REST 호출, 목업 데이터, 성능 기준 판정.

Streamlit 을 import 하지 않는다. 화면은 app.py 가 맡고, 이 모듈은 단위 테스트한다.
목업 함수(sample_*)는 forecast_api / dispatch_api 가 실제 응답을 주기 전까지만 쓴다.
"""

from __future__ import annotations

import math
import os
import random
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yaml

API_PREFIX = "/api/v1"

# 단위 환산 (정의상 상수라 config 에 두지 않는다)
KRW_PER_KWH_TO_KRW_PER_MWH = 1000
WON_PER_EOK = 1e8  # 1억 원


# ── 설정 ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Fuel:
    """발전원 1종 (목업 설비)."""

    code: str
    name: str
    capacity_mw: float
    fuel_cost_won_per_kwh: float
    co2_t_per_mwh: float


@dataclass(frozen=True)
class EssSpec:
    power_mw: float
    energy_mwh: float
    round_trip_efficiency: float


@dataclass(frozen=True)
class CrisisScenario:
    name: str
    demand_mult: float = 1.0
    capacity_delta_mw: float = 0.0
    solar_mult: float = 1.0


@dataclass(frozen=True)
class ApiSpec:
    """API 호출 timeout·캐시 TTL (초)."""

    timeout_short_s: float
    timeout_health_s: float
    timeout_scenarios_s: float
    stochastic_timeout_margin_s: float
    ttl_range_s: int
    ttl_scenarios_s: int
    ttl_stochastic_s: int
    ttl_health_s: int


@dataclass(frozen=True)
class MockSpec:
    """API 연동 전 목업 값 (config/dashboard.yaml 의 mock)."""

    seed: int
    forecast_seed: int
    demand_shape_mw: tuple[float, ...]
    solar_shape_mw: tuple[float, ...]
    weekday_factor: tuple[float, ...]
    wind_base_mw: float
    wind_amplitude_mw: float
    nuclear_availability: float
    hydro_base_ratio: float
    demand_noise: float
    solar_noise: float
    wind_noise_mw: float
    forecast_demand_actual_sd: float
    forecast_demand_sd: float
    forecast_demand_sd_growth: float
    forecast_solar_actual_sd: float
    forecast_solar_sd: float
    forecast_wind_actual_sd_mw: float
    forecast_wind_sd_mw: float
    pi_z: float
    milp_saving: float
    milp_latency_s: float
    milp_n_scenarios: int
    constraint_reserve_pct: float
    constraint_hydro_pct: float


@dataclass(frozen=True)
class Settings:
    """config/dashboard.yaml 을 읽은 결과."""

    reserve_warning_pct: float
    reserve_critical_pct: float
    targets: dict[str, float] = field(hash=False)
    timeout_s: float
    cache_ttl_s: int
    crisis: tuple[CrisisScenario, ...]
    fleet: tuple[Fuel, ...]
    ess: EssSpec
    api: ApiSpec
    forecast_history_days: int
    forecast_horizon_h: int
    # 페이지별 슬라이더 초기값 {"scenarios": {...}, "stochastic_test": {...}}
    page_defaults: dict[str, dict[str, float | bool]] = field(hash=False)
    mock: MockSpec

    @property
    def fuel(self) -> dict[str, Fuel]:
        return {f.code: f for f in self.fleet}


def _config_candidates(name: str = "dashboard.yaml", env: str = "DASHBOARD_CONFIG") -> list[Path]:
    """설정 파일을 찾을 경로. 환경변수 → 컨테이너 마운트 → 레포 루트 순."""
    paths: list[Path] = []
    if os.getenv(env):
        paths.append(Path(os.environ[env]))
    paths.append(Path("/app/config") / name)
    here = Path(__file__).resolve()
    if len(here.parents) > 2:
        paths.append(here.parents[2] / "config" / name)
    return paths


def load_fuel_co2(path: Path | None = None) -> dict[str, float]:
    """generators.yaml(C 담당) 의 연료별 배출계수 (tCO2/MWh). 못 찾으면 빈 dict.

    키는 collector trading_by_fuel 과 같은 연료 코드다. 같은 연료의 유닛은 값이 같다.
    값의 출처와 [미정] 항목(유류·무연탄은 출처 없음)은 generators.yaml 주석에 있다.
    """
    if path is None:
        path = next((p for p in _config_candidates("generators.yaml", "GENERATORS_CONFIG")
                     if p.exists()), None)
        if path is None:
            return {}
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return {g["fuel"]: float(g["co2_ton_per_mwh"]) for g in raw.get("generators", [])}


def load_unit_fuels(path: Path | None = None) -> dict[str, str]:
    """generators.yaml 의 유닛 id → 연료 코드 (MILP 유닛별 출력을 발전원으로 묶는 데 쓴다).

    못 찾으면 빈 dict. MilpResponse 의 units 에는 연료가 없어서 이 표가 필요하다.
    """
    if path is None:
        path = next((p for p in _config_candidates("generators.yaml", "GENERATORS_CONFIG")
                     if p.exists()), None)
        if path is None:
            return {}
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return {g["id"]: g["fuel"] for g in raw.get("generators", [])}


def cfg_value(node: Any, unit: str) -> Any:
    """{value, unit, source} 항목의 value. unit 이 기대와 다르면 멈춘다 (변환하지 않는다)."""
    if not isinstance(node, dict) or "value" not in node:
        raise ValueError(f"config 값은 {{value, unit, source}} 형식이어야 한다: {node!r}")
    if node.get("unit") != unit:
        raise ValueError(f"unit 이 '{unit}' 이어야 하는데 '{node.get('unit')}' 이다: {node!r}")
    return node["value"]


# CrisisScenario 필드 → unit
_CRISIS_UNITS = {"demand_mult": "ratio", "capacity_delta_mw": "MW", "solar_mult": "ratio"}
# 페이지 초기값 필드 → (unit, 형). compute_ws 같은 bool 은 숫자가 아니라 그대로 둔다
_PAGE_FIELDS: dict[str, tuple[str, type]] = {
    "n_scenarios": ("count", int), "n_samples": ("count", int), "seed": ("-", int),
    "demand_sigma_pct": ("%", float), "demand_ar": ("-", float),
    "solar_cloud_pct": ("%", float), "wind_sigma_pct": ("%", float),
    "time_limit_s": ("s", int), "mip_gap_pct": ("%", float),
}


def _page_defaults(raw: dict) -> dict[str, float | bool]:
    out: dict[str, float | bool] = {}
    for key, node in raw.items():
        if isinstance(node, bool):
            out[key] = node
        else:
            unit, cast = _PAGE_FIELDS[key]
            out[key] = cast(cfg_value(node, unit))
    return out


def _mock(raw: dict) -> MockSpec:
    def v(key: str, unit: str) -> Any:
        return cfg_value(raw[key], unit)

    return MockSpec(
        seed=int(v("seed", "-")),
        forecast_seed=int(v("forecast_seed", "-")),
        demand_shape_mw=tuple(v("demand_shape", "MW")),
        solar_shape_mw=tuple(v("solar_shape", "MW")),
        weekday_factor=tuple(v("weekday_factor", "ratio")),
        wind_base_mw=v("wind_base", "MW"),
        wind_amplitude_mw=v("wind_amplitude", "MW"),
        nuclear_availability=v("nuclear_availability", "ratio"),
        hydro_base_ratio=v("hydro_base_ratio", "ratio"),
        demand_noise=v("demand_noise", "ratio"),
        solar_noise=v("solar_noise", "ratio"),
        wind_noise_mw=v("wind_noise", "MW"),
        forecast_demand_actual_sd=v("forecast_demand_actual_sd", "ratio"),
        forecast_demand_sd=v("forecast_demand_sd", "ratio"),
        forecast_demand_sd_growth=v("forecast_demand_sd_growth", "ratio"),
        forecast_solar_actual_sd=v("forecast_solar_actual_sd", "ratio"),
        forecast_solar_sd=v("forecast_solar_sd", "ratio"),
        forecast_wind_actual_sd_mw=v("forecast_wind_actual_sd", "MW"),
        forecast_wind_sd_mw=v("forecast_wind_sd", "MW"),
        pi_z=v("pi_z", "-"),
        milp_saving=v("milp_saving", "ratio"),
        milp_latency_s=v("milp_latency", "s"),
        milp_n_scenarios=int(v("milp_n_scenarios", "count")),
        constraint_reserve_pct=v("constraint_reserve_pct", "%"),
        constraint_hydro_pct=v("constraint_hydro_pct", "%"),
    )


def load_settings(path: Path | None = None) -> Settings:
    """dashboard.yaml 을 읽는다. path 를 안 주면 _config_candidates() 에서 찾는다."""
    if path is None:
        path = next((p for p in _config_candidates() if p.exists()), None)
        if path is None:
            raise FileNotFoundError("config/dashboard.yaml 을 찾지 못했다")
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    api = raw["api"]
    return Settings(
        reserve_warning_pct=float(cfg_value(raw["alarm"]["reserve_warning_pct"], "%")),
        reserve_critical_pct=float(cfg_value(raw["alarm"]["reserve_critical_pct"], "%")),
        targets={
            k: float(cfg_value(v, "s" if k.endswith("_s") else "%"))
            for k, v in raw["targets"].items()
        },
        timeout_s=float(cfg_value(api["timeout"], "s")),
        cache_ttl_s=int(cfg_value(api["cache_ttl"], "s")),
        crisis=tuple(
            CrisisScenario(
                name=c["name"],
                **{k: cfg_value(c[k], unit) for k, unit in _CRISIS_UNITS.items() if k in c},
            )
            for c in raw["crisis_scenarios"]
        ),
        fleet=tuple(
            Fuel(
                code=f["code"], name=f["name"],
                capacity_mw=cfg_value(f["capacity_mw"], "MW"),
                fuel_cost_won_per_kwh=cfg_value(f["fuel_cost_won_per_kwh"], "KRW/kWh"),
                co2_t_per_mwh=cfg_value(f["co2_t_per_mwh"], "tCO2/MWh"),
            )
            for f in raw["fleet"]
        ),
        ess=EssSpec(
            power_mw=cfg_value(raw["ess"]["power_mw"], "MW"),
            energy_mwh=cfg_value(raw["ess"]["energy_mwh"], "MWh"),
            round_trip_efficiency=cfg_value(raw["ess"]["round_trip_efficiency"], "ratio"),
        ),
        api=ApiSpec(
            timeout_short_s=float(cfg_value(api["timeout_short"], "s")),
            timeout_health_s=float(cfg_value(api["timeout_health"], "s")),
            timeout_scenarios_s=cfg_value(api["timeout_scenarios"], "s"),
            stochastic_timeout_margin_s=float(cfg_value(api["stochastic_timeout_margin"], "s")),
            ttl_range_s=int(cfg_value(api["cache_ttl_range"], "s")),
            ttl_scenarios_s=int(cfg_value(api["cache_ttl_scenarios"], "s")),
            ttl_stochastic_s=int(cfg_value(api["cache_ttl_stochastic"], "s")),
            ttl_health_s=int(cfg_value(api["cache_ttl_health"], "s")),
        ),
        forecast_history_days=int(cfg_value(raw["forecast_request"]["history"], "day")),
        forecast_horizon_h=int(cfg_value(raw["forecast_request"]["horizon"], "h")),
        page_defaults={k: _page_defaults(v) for k, v in raw["page_defaults"].items()},
        mock=_mock(raw["mock"]),
    )


# ── 기준 판정 ────────────────────────────────────────────────────────────────

# True 면 "이상"(값이 클수록 좋음), False 면 "이하".
HIGHER_IS_BETTER: dict[str, bool] = {
    "mape_24h_pct": False,
    "mape_168h_pct": False,
    "pi_coverage_pct": True,
    "nmae_solar_pct": False,
    "nmae_wind_pct": False,
    "milp_saving_pct": True,
    "milp_latency_s": False,
    "vss_pct": True,
    "ess_peak_cut_pct": True,
    "crisis_reserve_pct": True,
}


def meets(settings: Settings, key: str, value: float) -> bool:
    """value 가 targets[key] 기준을 충족하는지."""
    target = settings.targets[key]
    return value >= target if HIGHER_IS_BETTER[key] else value <= target


def target_text(settings: Settings, key: str) -> str:
    """배지에 붙일 기준 문구. 예: '≥ 10%', '≤ 10초'."""
    unit = "초" if key.endswith("_s") else "%"
    op = "≥" if HIGHER_IS_BETTER[key] else "≤"
    return f"{op} {settings.targets[key]:g}{unit}"


def reserve_status(settings: Settings, reserve_pct: float) -> tuple[str, str]:
    """예비율 알람 등급 (라벨, css 클래스). 7% 미만 Warning, 5% 미만 Critical."""
    if reserve_pct < settings.reserve_critical_pct:
        return "Critical", "critical"
    if reserve_pct < settings.reserve_warning_pct:
        return "Warning", "warn"
    return "Normal", "ok"


# ── REST 호출 ────────────────────────────────────────────────────────────────


@dataclass
class ApiResult:
    """REST 호출 결과. error 가 None 이면 성공."""

    data: Any = None
    latency_s: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def service_urls() -> dict[str, str]:
    """서비스 주소. compose 가 환경변수로 넣고, 없으면 로컬 기본 포트."""
    return {
        "collector": os.getenv("COLLECTOR_URL", "http://localhost:8000"),
        "forecast_api": os.getenv("FORECAST_URL", "http://localhost:8001"),
        "dispatch_api": os.getenv("DISPATCH_URL", "http://localhost:8002"),
    }


def call_api(
    method: str,
    url: str,
    *,
    payload: dict | None = None,
    params: dict | None = None,
    timeout: float,
) -> ApiResult:
    """HTTP 호출 한 번. 예외를 올리지 않고 ApiResult 로 돌려준다 (화면이 목업으로 대체)."""
    start = time.perf_counter()
    try:
        res = requests.request(method, url, json=payload, params=params, timeout=timeout)
    except requests.RequestException as exc:
        return ApiResult(error=type(exc).__name__)
    latency = time.perf_counter() - start
    if res.status_code >= 400:
        return ApiResult(latency_s=latency, error=f"HTTP {res.status_code}")
    try:
        return ApiResult(data=res.json(), latency_s=latency)
    except ValueError:
        return ApiResult(latency_s=latency, error="JSON 아님")


def latest_row(result: ApiResult) -> dict | None:
    """collector /data/{name} 응답에서 마지막 행."""
    if not result.ok or not result.data or not result.data.get("rows"):
        return None
    return result.data["rows"][-1]


def latest_mix(result: ApiResult) -> dict[str, float]:
    """collector trading_by_fuel 응답에서 가장 최근 시각의 발전원별 MW.

    trade_mwh 는 1시간 거래량이라 그 시간의 평균 출력(MW)과 같은 값이다.
    """
    if not result.ok or not result.data:
        return {}
    rows = result.data.get("rows") or []
    if not rows:
        return {}
    last_ts = max(r["ts"] for r in rows)
    # collector 는 결측을 문자열 "None" 으로 내보낸다 (services/collector/domain.py MISSING)
    return {
        r["fuel"]: float(r["trade_mwh"])
        for r in rows
        if r["ts"] == last_ts and r["trade_mwh"] not in (None, "None")
    }


def parse_stochastic(data: dict) -> dict:
    """StochasticResponse(shared/schemas.py) 에서 화면에 쓰는 값만 꺼낸다."""
    vss = data["vss"]
    return {
        "status": data["status"],
        "solve_time_s": float(data["solve_time_s"]),
        "n_scenarios": int(data["n_scenarios"]),
        "recourse_won": float(data["expected_cost"]["recourse_won"]),
        "rp_won": float(vss["rp_won"]),
        "ev_won": float(vss["ev_won"]),
        "eev_won": float(vss["eev_won"]),
        "vss_won": float(vss["vss_won"]),
        "vss_pct": float(vss["vss_pct"]),
    }


def parse_stochastic_latest(data: dict) -> dict | None:
    """StochasticLatest(GET /dispatch/stochastic/latest) → 화면 값 + 계산 조건. 결과가 없으면 None.

    계산 중(running)이거나 실패(error)여도 이전 결과가 남아 있으면 그 값을 쓴다.
    """
    if not data.get("result"):
        return None
    out = parse_stochastic(data["result"])
    out.update({
        "state": data["state"],
        "computed_at": pd.Timestamp(data["computed_at"]) if data.get("computed_at") else None,
        "seed": int(data["seed"]),
        "reserve_ratio": float(data["reserve_ratio"]),
        "time_limit_s": float(data["time_limit_s"]),
    })
    return out


# 해가 있는 MILP 상태 (shared/schemas.py SolveStatus). infeasible·error 면 화면은 목업으로 둔다.
MILP_SOLVED = ("optimal", "feasible", "time_limit")


def parse_milp(data: dict, latency_s: float | None = None) -> dict | None:
    """MilpResponse(POST /dispatch/milp) → 화면 값. 해가 없거나 baseline 이 없으면 None.

    키는 sample_dispatch_summary 와 같게 맞추고 (milp_cost_won, rule_cost_won, saving_pct,
    latency_s, constraints) 계획 날짜·풀이 시간·ESS 를 덧붙인다. 비용은 연료비 + 기동비
    (+ 부하차단) 총비용이다 — 절감률(baseline.cost_reduction_pct)이 같은 정의를 쓴다.
    """
    if data["status"] not in MILP_SOLVED or not data.get("baseline"):
        return None
    base = data["baseline"]
    size = data["model_size"]
    return {
        "status": data["status"],
        "day": pd.Timestamp(data["timestamps"][0]).date(),
        "milp_cost_won": float(data["cost"]["total_won"]),
        "rule_cost_won": float(base["total_cost_won"]),
        "saving_pct": float(base["cost_reduction_pct"]),
        # 대시보드가 잰 HTTP 왕복 (데이터 로드 포함). 없으면 서버의 빌드+풀이 시간
        "latency_s": float(latency_s if latency_s is not None else size["total_time_s"]),
        "solve_s": float(size["solve_time_s"]),
        "constraints": {c["name"]: bool(c["ok"]) for c in data["constraints"]},
        "ess": data.get("ess"),
        "notes": list(data.get("notes", [])),
        "carbon_ton": float(data["carbon"]["total_ton"]),
        "carbon_hourly_ton": [float(v) for v in data["carbon"]["hourly_ton"]],
        "carbon_by_fuel_ton": {k: float(v) for k, v in data["carbon"]["by_fuel_ton"].items()},
        "carbon_intensity": float(data["carbon"]["intensity_ton_per_mwh"]),
        "rule_carbon_ton": float(base["carbon_total_ton"]),  # Rule-based 는 ESS 를 쓰지 않는다
        # Merit Order 스택용. units 는 include_units 로 요청했을 때만 있다
        "timestamps": [pd.Timestamp(ts) for ts in data["timestamps"]],
        "fuel_order": [m["fuel"] for m in sorted(data["merit_order"], key=lambda m: m["rank"])],
        "demand_mw": [float(h["demand_mw"]) for h in data["hours"]],
        "ess_mw": [float(h["ess_mw"]) for h in data["hours"]],
        "units": data.get("units") or [],
    }


def milp_fuel_stack(milp: dict, unit_fuel: dict[str, str]) -> pd.DataFrame:
    """MILP 유닛 x 시간 출력 → 발전원별 시간대 출력 (hour, fuel, mw) + ESS 충·방전.

    발전원 순서는 응답의 merit_order(연료비 순위)다. ESS 는 방전을 공급(+), 충전을 부하(−)로
    둔다 — 균형식 Σp + (방전 − 충전) = 순수요 (formulation.md 5.2). 연료를 모르는 유닛은
    "unknown" 으로 묶는다. units 가 없으면 빈 표.
    """
    if not milp["units"]:
        return pd.DataFrame(columns=["hour", "fuel", "mw"])
    start = milp["timestamps"][0]
    units = pd.DataFrame(milp["units"])
    units["hour"] = ((pd.to_datetime(units["timestamp"]) - start)
                     / pd.Timedelta(hours=1)).round().astype(int)
    units["fuel"] = units["unit_id"].map(unit_fuel).fillna("unknown")
    stack = units.groupby(["hour", "fuel"], as_index=False)["p_mw"].sum()
    stack = stack.rename(columns={"p_mw": "mw"})
    order = [*milp["fuel_order"], "unknown"]
    stack["rank"] = stack["fuel"].map({f: i for i, f in enumerate(order)})
    stack = stack.sort_values(["rank", "hour"]).drop(columns="rank")
    ess = pd.Series(milp["ess_mw"])
    extra = pd.DataFrame({
        "hour": list(ess.index) * 2,
        "fuel": ["ess_discharge"] * len(ess) + ["ess_charge"] * len(ess),
        "mw": list(ess.clip(lower=0)) + list(ess.clip(upper=0)),
    })
    if (extra["mw"] != 0).any():
        stack = pd.concat([stack, extra[extra["mw"] != 0]], ignore_index=True)
    return stack.reset_index(drop=True)


def _pct(after: float, before: float) -> float:
    """before 대비 증감률 (%). before 가 0 이면 0."""
    return (after - before) / before * 100 if before else 0.0


def carbon_tradeoff(with_ess: dict, no_ess: dict) -> dict:
    """같은 날 MILP 두 번(ESS 연계 / ESS 없음)의 parse_milp 결과로 탄소 비교 수치를 만든다.

    ess_pct  = ESS 연계가 배출을 얼마나 바꾸는가 (MILP+ESS vs MILP ESS 없음)
    rule_pct = 최적화가 배출을 얼마나 바꾸는가. Rule-based 는 ESS 가 없으므로 ESS 없는 MILP 와
               비교한다 (조건을 맞춘 비교). 양수면 MILP 가 더 많이 배출한다.
    """
    return {
        "with_ess_ton": with_ess["carbon_ton"],
        "no_ess_ton": no_ess["carbon_ton"],
        "rule_ton": no_ess["rule_carbon_ton"],
        "ess_pct": _pct(with_ess["carbon_ton"], no_ess["carbon_ton"]),
        "rule_pct": _pct(no_ess["carbon_ton"], no_ess["rule_carbon_ton"]),
    }


def carbon_plan_table(with_ess: dict, no_ess: dict) -> pd.DataFrame:
    """계획 3종의 배출량·비용 (비용-탄소 Trade-off 표). Rule-based 의 배출집약도는 응답에 없다."""
    rows = [
        ("Rule-based (ESS 없음)", no_ess["rule_carbon_ton"], None, no_ess["rule_cost_won"]),
        ("MILP (ESS 없음)", no_ess["carbon_ton"], no_ess["carbon_intensity"],
         no_ess["milp_cost_won"]),
        ("MILP + ESS", with_ess["carbon_ton"], with_ess["carbon_intensity"],
         with_ess["milp_cost_won"]),
    ]
    rule_ton, rule_cost = rows[0][1], rows[0][3]
    return pd.DataFrame([
        {
            "계획": name,
            "배출량(tCO2)": round(t),
            "Rule-based 대비 배출": round(_pct(t, rule_ton), 2),
            "배출집약도(tCO2/MWh)": None if i is None else round(i, 4),
            "비용(억원)": round(c / WON_PER_EOK, 1),
            "Rule-based 대비 비용": round(_pct(c, rule_cost), 2),
        }
        for name, t, i, c in rows
    ])


def milp_carbon_by_fuel(with_ess: dict, no_ess: dict, names: dict[str, str]) -> pd.DataFrame:
    """발전원별 배출량 (MILP ESS 없음 / MILP+ESS) 과 차이. names 는 발전원 코드 → 표시 이름."""
    fuels = list(dict.fromkeys([*no_ess["carbon_by_fuel_ton"], *with_ess["carbon_by_fuel_ton"]]))
    out = []
    for f in fuels:
        a = no_ess["carbon_by_fuel_ton"].get(f, 0.0)
        b = with_ess["carbon_by_fuel_ton"].get(f, 0.0)
        out.append({"발전원": names.get(f, f), "ESS 없음(tCO2)": round(a),
                    "ESS 연계(tCO2)": round(b), "차이(tCO2)": round(b - a)})
    return pd.DataFrame(out)


def milp_ess_frame(ess: dict) -> pd.DataFrame:
    """MilpResponse.ess 의 시간대별 충·방전·SoC → 표 (hour, ess_charge_mw, ess_discharge_mw,
    ess_soc_pct). 열 이름은 목업 시계열과 같게 둔다 (app.render_ess 가 같은 그리기 코드를 쓴다).
    """
    return pd.DataFrame({
        "hour": range(len(ess["charge_mw"])),
        "ess_charge_mw": [float(v) for v in ess["charge_mw"]],
        "ess_discharge_mw": [float(v) for v in ess["discharge_mw"]],
        "ess_soc_pct": [float(v) for v in ess["soc_pct"]],
    })


# ── collector 실측 (수요 게이지 · 실측 발전 믹스) ─────────────────────────────

# 실측 발전 믹스를 연료비 순으로 쌓을 때 쓰는 구분 (collector trading_by_fuel 코드).
# 가정: 재생·수력은 연료비 0원으로 맨 앞, 양수는 저장설비라 맨 뒤에 둔다.
ZERO_COST_FUELS = ("solar", "wind", "renewable", "hydro")
STORAGE_FUELS = ("pumped",)
# 이 셋이 24시간 모두 있어야 하루치 실측으로 쓴다 (최근 날짜는 일부 발전원이 늦게 들어온다)
CORE_FUELS = ("nuclear", "bituminous_coal", "lng")


def rows_frame(result: ApiResult, numeric: tuple[str, ...]) -> pd.DataFrame:
    """collector /data 응답 → DataFrame. 결측 표기("None")는 NaN 으로 바꾼다."""
    if not result.ok or not result.data:
        return pd.DataFrame()
    df = pd.DataFrame(result.data.get("rows") or [])
    for col in numeric:
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def latest_demand(result: ApiResult) -> dict | None:
    """collector power_demand(5분) 의 최신 수요와 1시간 전 수요 (없으면 None)."""
    df = rows_frame(result, ("demand_mw",))
    if df.empty:
        return None
    df = df.dropna(subset=["demand_mw"], ignore_index=True)
    if df.empty:
        return None
    df["ts"] = pd.to_datetime(df["ts"])
    last = df.iloc[-1]
    hour_ago = df.loc[df.ts == last.ts - pd.Timedelta(hours=1), "demand_mw"]
    return {
        "ts": last.ts,
        "mw": float(last.demand_mw),
        "prev_mw": float(hour_ago.iloc[0]) if len(hour_ago) else None,
    }


def latest_full_day(trading: pd.DataFrame) -> date | None:
    """trading_by_fuel 에서 CORE_FUELS 가 24시간 모두 있는 가장 최근 날."""
    if trading.empty:
        return None
    core = trading[trading.fuel.isin(CORE_FUELS) & trading.trade_mwh.notna()]
    counts = core.groupby([pd.to_datetime(core.ts).dt.date, "fuel"]).size().unstack()
    counts = counts.reindex(columns=list(CORE_FUELS))
    full = counts[(counts >= 24).all(axis=1)]
    return max(full.index) if len(full) else None


def merit_fuel_order(fuels: list[str], fuel_cost: pd.DataFrame) -> list[str]:
    """발전원을 연료비 순으로 정렬. 연료비는 fuel_cost 의 가장 최근 달 값.

    연료비가 없는 발전원은 ZERO_COST_FUELS 면 맨 앞, STORAGE_FUELS 면 맨 뒤,
    그 밖에는 연료비가 있는 발전원 뒤에 둔다.
    """
    cost: dict[str, float] = {}
    if not fuel_cost.empty:
        last = fuel_cost[fuel_cost.month == fuel_cost.month.max()]
        cost = dict(zip(last.fuel, last.cost_won_per_kwh, strict=True))

    def key(f: str) -> tuple[int, float]:
        if f in ZERO_COST_FUELS:
            return 0, 0.0
        if f in STORAGE_FUELS:
            return 3, 0.0
        return (1, cost[f]) if f in cost else (2, 0.0)

    return sorted(fuels, key=key)


def real_mix(trading: pd.DataFrame, demand: pd.DataFrame, day: date) -> pd.DataFrame:
    """그날 시간대별 발전원 출력(trade_mwh = 그 시간 평균 MW)과 실측 수요(5분 값의 시간 평균).

    반환: hour, fuel, mw, capacity_mw(설비용량) (긴 표) + 시간별 demand_mw 열.
    """
    t = trading[pd.to_datetime(trading.ts).dt.date == day].copy()
    t["hour"] = pd.to_datetime(t.ts).dt.hour
    if "capacity_mw" not in t:
        t["capacity_mw"] = float("nan")
    mix = t.groupby(["hour", "fuel"], as_index=False).agg(
        mw=("trade_mwh", lambda v: v.sum(min_count=1)), capacity_mw=("capacity_mw", "max"),
    ).dropna(subset=["mw"])
    if not demand.empty:
        d = demand.assign(hour=pd.to_datetime(demand.ts).dt.hour)
        hourly = d.groupby("hour")["demand_mw"].mean()
        mix["demand_mw"] = mix.hour.map(hourly)
    else:
        mix["demand_mw"] = float("nan")
    return mix


def real_carbon_by_hour(mix: pd.DataFrame, co2: dict[str, float]) -> pd.Series:
    """시간별 배출량 (tCO2) = 발전원별 실측 출력 x 배출계수.

    가정: 배출계수가 없는 발전원(태양광·풍력·기타 재생·양수)은 0 으로 본다.
    양수는 충전 전력을 만든 발전원 쪽에서 이미 배출이 잡힌다.
    """
    coef = mix.fuel.map(co2).fillna(0.0)
    return (mix.mw * coef).groupby(mix.hour).sum()


def real_carbon_by_fuel(
    mix: pd.DataFrame, co2: dict[str, float], names: dict[str, str], order: list[str],
) -> pd.DataFrame:
    """발전원별 하루 발전량(MWh)·배출계수·배출량(tCO2). 1시간 간격이라 MW 합 = MWh."""
    energy = mix.groupby("fuel").mw.sum()
    rows = []
    for f in order:
        if f not in energy:
            continue
        mwh = float(energy[f])
        coef = co2.get(f, 0.0)
        rows.append({
            "발전원": names.get(f, f),
            "발전량(MWh)": round(mwh),
            "배출계수(tCO2/MWh)": coef,
            "배출량(tCO2)": round(mwh * coef),
        })
    return pd.DataFrame(rows)


def real_utilization(mix: pd.DataFrame, hour: int, top: int = 6) -> pd.DataFrame:
    """그 시각 발전원별 출력과 이용률(출력 / 설비용량). 출력 큰 순 top 개 + 나머지는 '기타'."""
    h = mix[mix.hour == hour].sort_values("mw", ascending=False)
    head, rest = h.head(top), h.iloc[top:]
    out = head[["fuel", "mw", "capacity_mw"]].copy()
    if len(rest):
        out.loc[len(out)] = {
            "fuel": "기타", "mw": rest.mw.sum(), "capacity_mw": rest.capacity_mw.sum(),
        }
    out["util_pct"] = (out.mw / out.capacity_mw * 100).clip(upper=100).fillna(0.0)
    return out.reset_index(drop=True)


def parse_forecast(data: dict) -> tuple[pd.DataFrame, dict]:
    """forecast_api POST /forecast 응답 → (수요 예측 시계열, 화면 지표).

    응답 스키마는 services/forecast_api/schemas.py (shared 와 아직 다름, B 담당).
    미래 예측이라 실측 열은 없다. 지표는 forecast_api 가 보정 구간에서 잰 값이라
    목업의 forecast_metrics 와 정의가 다르다:
      - mape: 1시간 앞 예측 (lag 에 실측 사용). 24h·168h ahead 가 아니다
      - nmae: MAE / 실측 평균. 대시보드 기준(설비용량)과 다르다
      - coverage: 컨포멀 구간을 맞춘 같은 구간에서 잰 값
    """
    fcs = data["forecasts"]
    pts = fcs["demand"]["points"]
    fc = pd.DataFrame({
        "hour": range(len(pts)),
        "ts": pd.to_datetime([p["datetime"] for p in pts]),
        "demand_fc_mw": [float(p["predicted"]) for p in pts],
        "demand_lo_mw": [float(p["lower_bound"]) for p in pts],
        "demand_hi_mw": [float(p["upper_bound"]) for p in pts],
    })
    metrics = {
        "mape_1h_pct": float(fcs["demand"]["metrics"]["mape"]),
        "pi_coverage_pct": float(fcs["demand"]["metrics"]["coverage"]),
        "model": fcs["demand"]["model_used"],
    }
    for code in ("solar", "wind"):
        if code in fcs:
            metrics[f"nmae_{code}_pct"] = float(fcs[code]["metrics"]["nmae"])
            metrics[f"{code}_start"] = pd.Timestamp(fcs[code]["points"][0]["datetime"])
    return fc, metrics


# ── 목업 데이터 ──────────────────────────────────────────────────────────────

# 수요·태양광 곡선, 요일 계수, 노이즈 등 목업 값은 config/dashboard.yaml 의 mock (Settings.mock).


def _wind_shape(h: int, mock: MockSpec) -> float:
    return mock.wind_base_mw + mock.wind_amplitude_mw * math.sin(h / 24 * 4 * math.pi)


def merit_dispatch(
    net_demand: float, solar: float, wind: float, fleet: dict[str, Fuel], mock: MockSpec
) -> dict:
    """연료비 순 단순 급전. 원자력(기저) → 재생(must-run) → 석탄 → LNG → 수력(첨두).

    수력은 싸지만 저수량 제약이 있어 첨두에 쓴다고 가정한다. 못 채운 양은 shed.
    """
    nuclear = fleet["nuclear"].capacity_mw * mock.nuclear_availability
    hydro_base = fleet["hydro"].capacity_mw * mock.hydro_base_ratio  # 유입식 기저 출력
    remaining = max(net_demand - (nuclear + hydro_base + solar + wind), 0.0)
    coal = min(remaining, fleet["coal"].capacity_mw)
    remaining -= coal
    lng = min(remaining, fleet["lng"].capacity_mw)
    remaining -= lng
    hydro_peak = min(remaining, fleet["hydro"].capacity_mw - hydro_base)
    remaining -= hydro_peak
    return {
        "nuclear": nuclear, "hydro": hydro_base + hydro_peak, "solar": solar,
        "wind": wind, "coal": coal, "lng": lng, "shed": remaining,
    }


def plan_ess(demand: list[float], ess: EssSpec) -> tuple[list[float], list[float], list[float]]:
    """피크 셰이빙 스케줄. (충전 MW, 방전 MW, 방전 가능 에너지 MWh) 를 시간별로 돌려준다.

    1시간 간격 가정. 방전 에너지가 용량 안에 드는 가장 낮은 상한선까지 피크를 깎고,
    첫 방전 이전의 저부하 시간대에 충전한다. 왕복효율 손실은 충전 쪽에 전부 반영한다.
    충전이 모자라면 쓸 수 있는 에너지를 줄여 다시 계산한다.
    """
    n = len(demand)
    zeros = [0.0] * n
    power, eta = ess.power_mw, ess.round_trip_efficiency
    if power <= 0 or ess.energy_mwh <= 0 or n == 0:
        return zeros, zeros, zeros

    budget = ess.energy_mwh
    for _ in range(10):
        lo, hi = max(demand) - power, max(demand)
        for _ in range(60):
            mid = (lo + hi) / 2
            if sum(min(max(d - mid, 0.0), power) for d in demand) <= budget:
                hi = mid
            else:
                lo = mid
        cap_line = hi
        discharge = [min(max(d - cap_line, 0.0), power) for d in demand]
        if not any(discharge):
            return zeros, zeros, zeros

        first = next(i for i, x in enumerate(discharge) if x > 0)
        need = sum(discharge) / eta
        charge = [0.0] * n
        for i in sorted(range(first), key=lambda i: demand[i]):
            if need <= 1e-6:
                break
            c = min(power, need, max(cap_line - demand[i], 0.0))  # 충전이 새 피크를 만들지 않게
            charge[i] = c
            need -= c
        if need <= 1e-6:
            break
        budget = (sum(discharge) / eta - need) * eta  # 실제 충전 가능한 만큼으로 줄여 재시도

    stored, soc = 0.0, []
    for c, d in zip(charge, discharge):
        stored += c * eta - d
        soc.append(stored)
    return charge, discharge, soc


@dataclass(frozen=True)
class Adjust:
    """화면에서 조정하는 what-if 값. 기본값이면 설정 그대로다."""

    demand_pct: float = 100.0     # 수요 배율 (%)
    solar_pct: float = 100.0      # 태양광 출력 배율 (%)
    wind_pct: float = 100.0       # 풍력 출력 배율 (%)
    outage_mw: float = 0.0        # 원자력 탈락량 (MW)
    ess_power_mw: float | None = None      # None 이면 설정값
    ess_energy_mwh: float | None = None
    ess_rte: float | None = None

    def ess_spec(self, base: EssSpec) -> EssSpec:
        """설정 ESS 에 조정값을 덮어쓴 사양."""
        return EssSpec(
            power_mw=base.power_mw if self.ess_power_mw is None else self.ess_power_mw,
            energy_mwh=base.energy_mwh if self.ess_energy_mwh is None else self.ess_energy_mwh,
            round_trip_efficiency=(
                base.round_trip_efficiency if self.ess_rte is None else self.ess_rte
            ),
        )


def sample_timeseries(
    settings: Settings, seed: int | None = None, adjust: Adjust | None = None,
    demand_factor: float = 1.0,
) -> pd.DataFrame:
    """24시간 더미 급전 결과 (ESS 반영 / 미반영 둘 다). dispatch_api 연동 후 제거.

    demand_factor 는 요일 효과 같은 날짜별 수요 계수다 (sample_day 가 넘긴다).
    seed 를 안 주면 config 의 mock.seed.
    """
    adjust = adjust or Adjust()
    mock = settings.mock
    rng = random.Random(mock.seed if seed is None else seed)
    fleet = dict(settings.fuel)
    nuke = fleet["nuclear"]
    fleet["nuclear"] = Fuel(
        nuke.code, nuke.name, max(nuke.capacity_mw - adjust.outage_mw, 0.0),
        nuke.fuel_cost_won_per_kwh, nuke.co2_t_per_mwh,
    )
    ess = adjust.ess_spec(settings.ess)

    demand = [
        d * (1 + rng.uniform(-mock.demand_noise, mock.demand_noise))
        * adjust.demand_pct / 100 * demand_factor
        for d in mock.demand_shape_mw
    ]
    solar = [
        s * (1 + rng.uniform(-mock.solar_noise, mock.solar_noise)) * adjust.solar_pct / 100
        for s in mock.solar_shape_mw
    ]
    wind = [
        max(_wind_shape(h, mock) + rng.uniform(-mock.wind_noise_mw, mock.wind_noise_mw), 0.0)
        * adjust.wind_pct / 100
        for h in range(24)
    ]
    charge, discharge, soc = plan_ess(demand, ess)

    firm_mw = sum(fleet[c].capacity_mw for c in ("nuclear", "hydro", "coal", "lng"))
    rows = []
    for h in range(24):
        net = demand[h] + charge[h] - discharge[h]
        with_ess = merit_dispatch(net, solar[h], wind[h], fleet, mock)
        no_ess = merit_dispatch(demand[h], solar[h], wind[h], fleet, mock)
        available = firm_mw + solar[h] + wind[h] + ess.power_mw
        rows.append({
            "hour": h,
            "demand_mw": demand[h],
            **{f"{code}_mw": mw for code, mw in with_ess.items()},
            "ess_charge_mw": charge[h],
            "ess_discharge_mw": discharge[h],
            "ess_soc_pct": soc[h] / ess.energy_mwh * 100 if ess.energy_mwh else 0.0,
            "net_load_mw": net,
            "carbon_ton": _carbon(with_ess, fleet),
            "carbon_no_ess_ton": _carbon(no_ess, fleet),
            "fuel_cost_won": sum(
                with_ess[c] * fleet[c].fuel_cost_won_per_kwh * KRW_PER_KWH_TO_KRW_PER_MWH
                for c in fleet
            ),
            "available_capacity_mw": available,
            "reserve_pct": (available - demand[h]) / demand[h] * 100,
        })
    return pd.DataFrame(rows)


MAX_PERIOD_DAYS = 31


def sample_day(settings: Settings, day: date, adjust: Adjust | None = None) -> pd.DataFrame:
    """하루치 목업. 날짜로 seed 를 고정하고(같은 날은 늘 같은 값) 요일별 수요 계수를 곱한다.

    ESS 는 하루 단위로 충전 → 방전을 계획한다 (일일 피크 셰이빙).
    """
    df = sample_timeseries(
        settings, seed=day.toordinal(), adjust=adjust,
        demand_factor=settings.mock.weekday_factor[day.weekday()],
    )
    df.insert(0, "date", day)
    df.insert(1, "ts", pd.Timestamp(day) + pd.to_timedelta(df.hour, unit="h"))
    return df


def clamp_period(start: date, end: date) -> tuple[date, date]:
    """분석 기간 정리: 순서를 맞추고 최대 MAX_PERIOD_DAYS 일로 자른다 (끝 날짜 기준)."""
    if end < start:
        start, end = end, start
    return max(start, end - timedelta(days=MAX_PERIOD_DAYS - 1)), end


def sample_period(
    settings: Settings, start: date, end: date, adjust: Adjust | None = None
) -> pd.DataFrame:
    """분석 기간(start~end, 양끝 포함)의 시간별 목업을 이어 붙인다."""
    start, end = clamp_period(start, end)
    days = (end - start).days + 1
    return pd.concat(
        [sample_day(settings, start + timedelta(days=i), adjust) for i in range(days)],
        ignore_index=True,
    )


def daily_summary(df: pd.DataFrame) -> pd.DataFrame:
    """기간 시계열 → 일별 탄소 배출·ESS 효과·피크 감소율 표."""
    g = df.groupby("date", sort=True)
    out = pd.DataFrame({
        "배출(tCO2)": g.carbon_ton.sum(),
        "ESS 없음(tCO2)": g.carbon_no_ess_ton.sum(),
        "원 수요 피크(MW)": g.demand_mw.max(),
        "순부하 피크(MW)": g.net_load_mw.max(),
    })
    out["ESS 배출 변화(%)"] = (out["배출(tCO2)"] / out["ESS 없음(tCO2)"] - 1) * 100
    out["피크 감소율(%)"] = (1 - out["순부하 피크(MW)"] / out["원 수요 피크(MW)"]) * 100
    out = out.round({"배출(tCO2)": 0, "ESS 없음(tCO2)": 0, "원 수요 피크(MW)": 0,
                     "순부하 피크(MW)": 0, "ESS 배출 변화(%)": 2, "피크 감소율(%)": 1})
    return out.reset_index().rename(columns={"date": "날짜"})


def _carbon(outputs: dict, fleet: dict[str, Fuel]) -> float:
    return sum(outputs[c] * fleet[c].co2_t_per_mwh for c in fleet)


def sample_forecast(
    settings: Settings, seed: int | None = None, horizon_h: int | None = None
) -> pd.DataFrame:
    """더미 예측 (실측·예측·90% PI). 멀리 볼수록 오차를 키운다. forecast_api 연동 후 제거.

    seed·horizon_h 를 안 주면 config 의 mock.forecast_seed, forecast_request.horizon.
    """
    m = settings.mock
    rng = random.Random(m.forecast_seed if seed is None else seed)
    horizon_h = settings.forecast_horizon_h if horizon_h is None else horizon_h
    solar_cap = settings.fuel["solar"].capacity_mw
    rows = []
    for h in range(horizon_h):
        base = m.demand_shape_mw[h % 24] * m.weekday_factor[(h // 24) % 7]
        actual = base * (1 + rng.gauss(0, m.forecast_demand_actual_sd))
        sd = m.forecast_demand_sd + m.forecast_demand_sd_growth * h / horizon_h
        fc = actual * (1 + rng.gauss(0, sd))
        solar_act = m.solar_shape_mw[h % 24] * max(1 + rng.gauss(0, m.forecast_solar_actual_sd), 0)
        solar_fc = (
            max(solar_act + rng.gauss(0, m.forecast_solar_sd * solar_cap), 0) if solar_act else 0.0
        )
        wind_act = max(_wind_shape(h, m) + rng.gauss(0, m.forecast_wind_actual_sd_mw), 0.0)
        wind_fc = max(wind_act + rng.gauss(0, m.forecast_wind_sd_mw), 0.0)
        rows.append({
            "hour": h,
            "demand_actual_mw": actual,
            "demand_fc_mw": fc,
            "demand_lo_mw": fc * (1 - m.pi_z * sd),
            "demand_hi_mw": fc * (1 + m.pi_z * sd),
            "solar_actual_mw": solar_act,
            "solar_fc_mw": solar_fc,
            "wind_actual_mw": wind_act,
            "wind_fc_mw": wind_fc,
        })
    return pd.DataFrame(rows)


def forecast_metrics(fc: pd.DataFrame, settings: Settings) -> dict[str, float]:
    """예측 성능 지표. MAPE(24h/168h), 90% PI Coverage, 설비용량 기준 nMAE."""

    def mape(part: pd.DataFrame) -> float:
        err = (part.demand_fc_mw - part.demand_actual_mw).abs() / part.demand_actual_mw
        return float(err.mean() * 100)

    def nmae(code: str) -> float:
        err = (fc[f"{code}_fc_mw"] - fc[f"{code}_actual_mw"]).abs().mean()
        return float(err / settings.fuel[code].capacity_mw * 100)

    inside = fc.demand_actual_mw.between(fc.demand_lo_mw, fc.demand_hi_mw)
    return {
        "mape_24h_pct": mape(fc.head(24)),
        "mape_168h_pct": mape(fc),
        "pi_coverage_pct": float(inside.mean() * 100),
        "nmae_solar_pct": nmae("solar"),
        "nmae_wind_pct": nmae("wind"),
    }


def sample_dispatch_summary(df: pd.DataFrame, settings: Settings) -> dict:
    """MILP 요약 목업. dispatch_api 가 응답하지 않을 때 쓴다 (응답이 있으면 parse_milp)."""
    m = settings.mock
    milp_cost = float(df.fuel_cost_won.sum())
    rule_cost = milp_cost / (1 - m.milp_saving)
    return {
        "milp_cost_won": milp_cost,
        "rule_cost_won": rule_cost,
        "saving_pct": (rule_cost - milp_cost) / rule_cost * 100,
        "latency_s": m.milp_latency_s,
        "n_scenarios": m.milp_n_scenarios,
        "constraints": {
            "수급균형": True, f"예비율≥{m.constraint_reserve_pct:g}%": True, "출력 상하한": True,
            "램프율": True, "최소기동정지": True, f"수력저수율<{m.constraint_hydro_pct:g}%": True,
        },
    }


def sample_stochastic() -> dict:
    """StochasticResponse 모양의 목업 (api_spec.md 샘플과 같은 숫자)."""
    return {
        "status": "optimal",
        "solve_time_s": 7.8,
        "n_scenarios": 10,
        "expected_cost": {"recourse_won": 4.2e8},
        "vss": {
            "rp_won": 1.593e10, "ev_won": 1.548e10, "eev_won": 1.651e10,
            "vss_won": 5.8e8, "vss_pct": 5.8e8 / 1.651e10 * 100,
        },
    }


# ── 분석 계산 ────────────────────────────────────────────────────────────────


def peak_reduction(df: pd.DataFrame) -> tuple[float, float, float]:
    """(원 수요 피크, ESS 반영 순부하 피크, 감소율 %)."""
    before = float(df.demand_mw.max())
    after = float(df.net_load_mw.max())
    return before, after, (before - after) / before * 100


def apply_scenario(row: pd.Series, scenario: CrisisScenario) -> tuple[float, float]:
    """위기 시나리오를 (수요, 가용용량)에 반영한다."""
    demand = row.demand_mw * scenario.demand_mult
    capacity = row.available_capacity_mw + scenario.capacity_delta_mw
    capacity -= row.solar_mw * (1 - scenario.solar_mult)
    return demand, capacity


def crisis_ok_column(settings: Settings) -> str:
    """crisis_table 의 유지 여부 열 이름. 예: "5% 유지"."""
    return f"{settings.targets['crisis_reserve_pct']:g}% 유지"


def crisis_table(df: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """시나리오별 하루 중 최저 예비율과 그 시각, 기준(crisis_reserve_pct) 유지 여부."""
    out = []
    for sc in settings.crisis:
        reserves = []
        for _, row in df.iterrows():
            demand, capacity = apply_scenario(row, sc)
            reserves.append((capacity - demand) / demand * 100)
        worst = min(range(len(reserves)), key=reserves.__getitem__)
        out.append({
            "시나리오": sc.name,
            "최저 예비율(%)": round(reserves[worst], 1),
            "시각": f"{int(df.hour.iloc[worst])}시",
            crisis_ok_column(settings): meets(settings, "crisis_reserve_pct", reserves[worst]),
        })
    return pd.DataFrame(out)


def carbon_by_fuel(df: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """발전원별 일간 발전량(MWh)·배출계수·배출량(tCO2). 1시간 간격이라 MW 합 = MWh."""
    return pd.DataFrame([
        {
            "발전원": f.name,
            "발전량(MWh)": round(float(df[f"{f.code}_mw"].sum())),
            "배출계수(tCO2/MWh)": f.co2_t_per_mwh,
            "배출량(tCO2)": round(float(df[f"{f.code}_mw"].sum()) * f.co2_t_per_mwh),
        }
        for f in settings.fleet
    ])


# ── 읽기 쉬운 단위 ───────────────────────────────────────────────────────────


def fmt_power(mw: float, compact: bool = True) -> str:
    """전력. compact 면 1,000MW 이상을 GW 로 줄인다. 예: 84,780 MW → '84.8 GW'."""
    if compact and abs(mw) >= 1000:
        return f"{mw / 1000:,.1f} GW"
    return f"{mw:,.0f} MW"


def fmt_energy(mwh: float, compact: bool = True) -> str:
    """전력량. compact 면 GWh 로 줄인다."""
    if compact and abs(mwh) >= 1000:
        return f"{mwh / 1000:,.1f} GWh"
    return f"{mwh:,.0f} MWh"


def fmt_won(won: float, compact: bool = True) -> str:
    """금액. compact 면 조·억·만 단위. 예: 1.593e10 → '159.3억원'."""
    if not compact:
        return f"{won:,.0f}원"
    sign, v = ("-" if won < 0 else ""), abs(won)
    for unit, size in (("조", 1e12), ("억", WON_PER_EOK), ("만", 1e4)):
        if v >= size:
            return f"{sign}{v / size:,.1f}{unit}원"
    return f"{sign}{v:,.0f}원"


def fmt_ton(ton: float, compact: bool = True) -> str:
    """배출량. compact 면 천 t 단위. 예: 747,029 → '747.0천 tCO2'."""
    if compact and abs(ton) >= 1000:
        return f"{ton / 1000:,.1f}천 tCO2"
    return f"{ton:,.0f} tCO2"


# ── 기본값 대비 비교 ─────────────────────────────────────────────────────────


def kpi_snapshot(df: pd.DataFrame) -> dict[str, float]:
    """조정 전/후를 비교할 핵심 지표."""
    before, after, cut = peak_reduction(df)
    return {
        "peak_mw": before,
        "net_peak_mw": after,
        "ess_peak_cut_pct": cut,
        "min_reserve_pct": float(df.reserve_pct.min()),
        "shed_mwh": float(df.shed_mw.sum()),
        "fuel_cost_won": float(df.fuel_cost_won.sum()),
        "carbon_ton": float(df.carbon_ton.sum()),
    }


# (지표 키, 표시 이름, 단위 종류, 판정 기준 키 또는 None)
COMPARE_ROWS: tuple[tuple[str, str, str, str | None], ...] = (
    ("peak_mw", "원 수요 피크", "power", None),
    ("net_peak_mw", "ESS 반영 순부하 피크", "power", None),
    ("ess_peak_cut_pct", "피크 감소율", "pct", "ess_peak_cut_pct"),
    ("min_reserve_pct", "하루 최저 예비율", "pct", "crisis_reserve_pct"),
    ("shed_mwh", "공급 부족량", "energy", None),
    ("fuel_cost_won", "일간 연료비", "won", None),
    ("carbon_ton", "일간 탄소 배출", "ton", None),
)


def _fmt(kind: str, value: float, compact: bool) -> str:
    return {
        "power": lambda v: fmt_power(v, compact),
        "energy": lambda v: fmt_energy(v, compact),
        "won": lambda v: fmt_won(v, compact),
        "ton": lambda v: fmt_ton(v, compact),
        "pct": lambda v: f"{v:.1f}%",
    }[kind](value)


def compare_table(
    settings: Settings, base: pd.DataFrame, adjusted: pd.DataFrame, compact: bool = True
) -> pd.DataFrame:
    """기본값 대비 조정 결과 표. 변화는 % 지표면 %p, 나머지는 증감률(%)로 적는다."""
    b, a = kpi_snapshot(base), kpi_snapshot(adjusted)
    rows = []
    for key, name, kind, target in COMPARE_ROWS:
        diff = a[key] - b[key]
        if kind == "pct":
            change = f"{diff:+.1f}%p"
        elif b[key]:
            change = f"{diff / b[key] * 100:+.1f}%"
        else:
            change = "—" if not diff else f"+{_fmt(kind, diff, compact)}"
        verdict = ""
        if target is not None:
            verdict = "✓ 충족" if meets(settings, target, a[key]) else "✗ 미달"
        rows.append({
            "지표": name,
            "기본": _fmt(kind, b[key], compact),
            "조정 후": _fmt(kind, a[key], compact),
            "변화": change,
            "기준": target_text(settings, target) if target else "",
            "판정": verdict,
        })
    return pd.DataFrame(rows)


def hourly_table(settings: Settings, df: pd.DataFrame) -> pd.DataFrame:
    """시간대 상세표 (한글 컬럼, MW 원값). 화면 표시와 CSV 내려받기에 같이 쓴다."""
    out = pd.DataFrame({"시각": [f"{h:02d}:00" for h in df.hour]})
    out["수요(MW)"] = df.demand_mw.round(0)
    for f in settings.fleet:
        out[f"{f.name}(MW)"] = df[f"{f.code}_mw"].round(0)
    out["ESS(MW, +방전)"] = (df.ess_discharge_mw - df.ess_charge_mw).round(0)
    out["SoC(%)"] = df.ess_soc_pct.round(1)
    out["예비율(%)"] = df.reserve_pct.round(1)
    out["공급부족(MW)"] = df.shed_mw.round(0)
    out["배출(tCO2)"] = df.carbon_ton.round(0)
    out["연료비(억원)"] = (df.fuel_cost_won / WON_PER_EOK).round(2)
    return out
