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

    @property
    def fuel(self) -> dict[str, Fuel]:
        return {f.code: f for f in self.fleet}


def _config_candidates() -> list[Path]:
    """설정 파일을 찾을 경로. 환경변수 → 컨테이너 마운트 → 레포 루트 순."""
    paths: list[Path] = []
    if os.getenv("DASHBOARD_CONFIG"):
        paths.append(Path(os.environ["DASHBOARD_CONFIG"]))
    paths.append(Path("/app/config/dashboard.yaml"))
    here = Path(__file__).resolve()
    if len(here.parents) > 2:
        paths.append(here.parents[2] / "config" / "dashboard.yaml")
    return paths


def load_settings(path: Path | None = None) -> Settings:
    """dashboard.yaml 을 읽는다. path 를 안 주면 _config_candidates() 에서 찾는다."""
    if path is None:
        path = next((p for p in _config_candidates() if p.exists()), None)
        if path is None:
            raise FileNotFoundError("config/dashboard.yaml 을 찾지 못했다")
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Settings(
        reserve_warning_pct=float(raw["alarm"]["reserve_warning_pct"]),
        reserve_critical_pct=float(raw["alarm"]["reserve_critical_pct"]),
        targets={k: float(v) for k, v in raw["targets"].items()},
        timeout_s=float(raw["api"]["timeout_s"]),
        cache_ttl_s=int(raw["api"]["cache_ttl_s"]),
        crisis=tuple(CrisisScenario(**c) for c in raw["crisis_scenarios"]),
        fleet=tuple(Fuel(**f) for f in raw["fleet"]),
        ess=EssSpec(**raw["ess"]),
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
    timeout: float = 12.0,
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
    """collector generation_by_fuel 응답에서 가장 최근 시각의 발전원별 MW."""
    if not result.ok or not result.data:
        return {}
    rows = result.data.get("rows") or []
    if not rows:
        return {}
    last_ts = max(r["ts"] for r in rows)
    return {r["fuel"]: float(r["mw"]) for r in rows if r["ts"] == last_ts and r["mw"] is not None}


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


# ── 목업 데이터 ──────────────────────────────────────────────────────────────

# 하루 수요 곡선 (전형적인 한국 부하 패턴을 본뜬 토이 값, MW)
DEMAND_SHAPE_MW = [
    52000, 50000, 49000, 48000, 49000, 52000,
    58000, 66000, 72000, 76000, 79000, 81000,
    82000, 80000, 78000, 77000, 78000, 80000,
    84000, 86000, 83000, 74000, 65000, 57000,
]
SOLAR_SHAPE_MW = [
    0, 0, 0, 0, 0, 200,
    1200, 3600, 6400, 8800, 10400, 11200,
    11600, 11200, 10000, 8000, 5600, 2800,
    800, 100, 0, 0, 0, 0,
]
WEEKDAY_FACTOR = [1.0, 1.02, 1.02, 1.01, 1.0, 0.9, 0.85]  # 월~일


def _wind_shape(h: int) -> float:
    return 800 + 700 * math.sin(h / 24 * 4 * math.pi)


def merit_dispatch(net_demand: float, solar: float, wind: float, fleet: dict[str, Fuel]) -> dict:
    """연료비 순 단순 급전. 원자력(기저) → 재생(must-run) → 석탄 → LNG → 수력(첨두).

    수력은 싸지만 저수량 제약이 있어 첨두에 쓴다고 가정한다. 못 채운 양은 shed.
    """
    nuclear = fleet["nuclear"].capacity_mw * 0.95
    hydro_base = fleet["hydro"].capacity_mw * 0.12  # 유입식 기저 출력
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
    settings: Settings, seed: int = 42, adjust: Adjust | None = None,
    demand_factor: float = 1.0,
) -> pd.DataFrame:
    """24시간 더미 급전 결과 (ESS 반영 / 미반영 둘 다). dispatch_api 연동 후 제거.

    demand_factor 는 요일 효과 같은 날짜별 수요 계수다 (sample_day 가 넘긴다).
    """
    adjust = adjust or Adjust()
    rng = random.Random(seed)
    fleet = dict(settings.fuel)
    nuke = fleet["nuclear"]
    fleet["nuclear"] = Fuel(
        nuke.code, nuke.name, max(nuke.capacity_mw - adjust.outage_mw, 0.0),
        nuke.fuel_cost_won_per_kwh, nuke.co2_t_per_mwh,
    )
    ess = adjust.ess_spec(settings.ess)

    demand = [
        d * (1 + rng.uniform(-0.015, 0.015)) * adjust.demand_pct / 100 * demand_factor
        for d in DEMAND_SHAPE_MW
    ]
    solar = [
        s * (1 + rng.uniform(-0.05, 0.05)) * adjust.solar_pct / 100 for s in SOLAR_SHAPE_MW
    ]
    wind = [
        max(_wind_shape(h) + rng.uniform(-300, 300), 0.0) * adjust.wind_pct / 100
        for h in range(24)
    ]
    charge, discharge, soc = plan_ess(demand, ess)

    firm_mw = sum(fleet[c].capacity_mw for c in ("nuclear", "hydro", "coal", "lng"))
    rows = []
    for h in range(24):
        net = demand[h] + charge[h] - discharge[h]
        with_ess = merit_dispatch(net, solar[h], wind[h], fleet)
        no_ess = merit_dispatch(demand[h], solar[h], wind[h], fleet)
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
                with_ess[c] * fleet[c].fuel_cost_won_per_kwh * 1000 for c in fleet
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
        demand_factor=WEEKDAY_FACTOR[day.weekday()],
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


def sample_forecast(settings: Settings, seed: int = 7, horizon_h: int = 168) -> pd.DataFrame:
    """168h 더미 예측 (실측·예측·90% PI). 멀리 볼수록 오차를 키운다. forecast_api 연동 후 제거."""
    rng = random.Random(seed)
    solar_cap = settings.fuel["solar"].capacity_mw
    rows = []
    for h in range(horizon_h):
        base = DEMAND_SHAPE_MW[h % 24] * WEEKDAY_FACTOR[(h // 24) % 7]
        actual = base * (1 + rng.gauss(0, 0.01))
        sd = 0.02 + 0.03 * h / horizon_h
        fc = actual * (1 + rng.gauss(0, sd))
        solar_act = SOLAR_SHAPE_MW[h % 24] * max(1 + rng.gauss(0, 0.1), 0)
        solar_fc = max(solar_act + rng.gauss(0, 0.06 * solar_cap), 0) if solar_act else 0.0
        wind_act = max(_wind_shape(h) + rng.gauss(0, 200), 0.0)
        wind_fc = max(wind_act + rng.gauss(0, 350), 0.0)
        rows.append({
            "hour": h,
            "demand_actual_mw": actual,
            "demand_fc_mw": fc,
            "demand_lo_mw": fc * (1 - 1.645 * sd),
            "demand_hi_mw": fc * (1 + 1.645 * sd),
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


def sample_dispatch_summary(df: pd.DataFrame) -> dict:
    """MILP 요약 목업. /dispatch/milp 응답 계약(C 담당)이 정해지면 파서로 교체."""
    milp_cost = float(df.fuel_cost_won.sum())
    rule_cost = milp_cost / (1 - 0.124)
    return {
        "milp_cost_won": milp_cost,
        "rule_cost_won": rule_cost,
        "saving_pct": (rule_cost - milp_cost) / rule_cost * 100,
        "latency_s": 3.2,
        "n_scenarios": 12,
        "constraints": {
            "수급균형": True, "예비율≥10%": True, "출력 상하한": True,
            "램프율": True, "최소기동정지": True, "수력저수율<30%": True,
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


def crisis_table(df: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """시나리오별 하루 중 최저 예비율과 그 시각, 5% 유지 여부."""
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
            "5% 유지": meets(settings, "crisis_reserve_pct", reserves[worst]),
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
    for unit, size in (("조", 1e12), ("억", 1e8), ("만", 1e4)):
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
    out["연료비(억원)"] = (df.fuel_cost_won / 1e8).round(2)
    return out
