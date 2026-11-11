"""불확실성 시나리오 생성기 (docs/formulation.md 7절).

forecast_api 의 예측과 90% 예측구간에서 출발한다.

1. σ (시간별 표준편차, MW)
       σ_t = (구간 반폭 m_t / z) x sigma_scale,   z = 정규분포 양측 pi_confidence 분위 (90% → 1.645)
   forecast_api 의 구간은 conformal 이다 (반폭 = 검증 구간 |오차| 의 90% 분위). 오차가 평균 0
   정규분포면 반폭 = z·σ 라서 이렇게 되돌린다. 기준 예측에 구간이 없으면 (config 의 base_profile)
   fallback_sigma(%) x 예측값.
2. 표본: 변수마다 단위분산 정상 AR(1) 경로 u_t 를 만들고 오차 e_t = σ_t·u_t 를 더한다. 시간별 σ 가
   그대로 유지되므로 표본의 시간별 90% 범위가 forecast_api 구간과 같다. 세 변수의 오차는 서로
   독립이다 (가정). 수요는 0 이상, 태양광·풍력은 0 ~ 설비용량으로 자른다.
3. 축약: fast forward selection (Heitsch & Römisch 2003). 순부하 24h 경로 사이의 유클리드 거리로
   표본 n_samples 개 중 n_scenarios 개를 고르고, 안 뽑힌 표본의 확률은 가장 가까운 뽑힌 표본에
   더한다.
4. 라벨·검증: 7.6·7.7.

모든 숫자 파라미터는 config/stochastic.yaml 의 scenario_generator 블록에서 읽는다
(모듈을 불러올 때 한 번).
"""

from __future__ import annotations

import math
from pathlib import Path
from statistics import NormalDist
from typing import Any

import numpy as np

from shared.schemas import (
    Band,
    ScenarioCheck,
    ScenarioDetail,
    ScenarioProfile,
    ScenarioRequest,
    ScenarioSetResponse,
)
from units import cfg_value, read_stochastic_yaml

# ScenarioRequest 필드 → (config 키, unit, 형). 요청에서 비운(None) 필드를 이 값으로 채운다
REQUEST_FIELDS = {
    "n_scenarios": ("n_scenarios", "count", int),
    "n_samples": ("n_samples", "count", int),
    "seed": ("seed", "-", int),
    "sigma_scale": ("sigma_scale", "-", float),
    "demand_ar": ("demand_ar", "-", float),
    "solar_ar": ("solar_ar", "-", float),
    "wind_ar": ("wind_ar", "-", float),
    "solar_capacity_mw": ("solar_capacity", "MW", float),
    "wind_capacity_mw": ("wind_capacity", "MW", float),
}
TARGETS = ("demand", "solar", "wind")


def load_generator_config(path: Path | None = None) -> dict[str, Any]:
    """scenario_generator 블록 (단위 확인 후 value 만)."""
    raw = read_stochastic_yaml(path)["scenario_generator"]
    cfg: dict[str, Any] = {
        field: cast(cfg_value(raw[key], unit))
        for field, (key, unit, cast) in REQUEST_FIELDS.items()
    }
    for key, unit, cast in [
        ("pi_confidence", "ratio", float),
        ("label_demand", "%", float), ("label_solar", "%", float), ("label_wind", "%", float),
        ("check_min_scenarios", "count", int), ("check_prob_sum_tol", "-", float),
        ("check_demand_bias", "%", float), ("check_pi_coverage_tol", "%p", float),
        ("check_net_energy_keep", "%", float),
        ("check_tail_upper", "percentile", float), ("check_tail_lower", "percentile", float),
    ]:
        cfg[key] = cast(cfg_value(raw[key], unit))
    fallback = cfg_value(raw["fallback_sigma"], "%")
    cfg["fallback_sigma"] = {t: float(fallback[t]) for t in TARGETS}
    cfg["band_percentiles"] = list(cfg_value(raw["band_percentiles"], "percentile"))
    cfg["base_profile"] = ScenarioProfile(**cfg_value(raw["base_profile"], "MW"), source="config")
    return cfg


CFG = load_generator_config()

# forecast_api 를 쓸 수 없을 때의 기준 예측 (MW, 구간 없음).
# 기준일·출처는 config 의 base_profile.source.
# forecast_api 예측은 forecast_client.resolve_base 가 받아 요청의 base 로 넣는다.
DEFAULT_BASE: ScenarioProfile = CFG["base_profile"]

# 구간 반폭 → σ 환산 계수. 90% 양측이면 1.645
Z_PI = NormalDist().inv_cdf(0.5 + CFG["pi_confidence"] / 2)


def with_defaults(req: ScenarioRequest) -> ScenarioRequest:
    """요청에서 비운(None) 필드를 config 기본값으로 채운다."""
    missing = {f: CFG[f] for f in REQUEST_FIELDS if getattr(req, f) is None}
    return req.model_copy(update=missing) if missing else req


# ── 표본 ─────────────────────────────────────────────────────────────────────


def _arrays(base: ScenarioProfile, field: str) -> dict[str, np.ndarray | None]:
    """target → base 의 {target}{field} 배열 (없으면 None)."""
    out = {}
    for t in TARGETS:
        values = getattr(base, f"{t}{field}")
        out[t] = None if values is None else np.asarray(values, dtype=float)
    return out


def sigmas(req: ScenarioRequest, base: ScenarioProfile) -> dict[str, np.ndarray]:
    """target → 시간별 σ (MW). 구간 반폭이 있으면 반폭 / z, 없으면 fallback_sigma(%) x 예측값."""
    req = with_defaults(req)
    pred, margin = _arrays(base, "_mw"), _arrays(base, "_margin_mw")
    out = {}
    for t in TARGETS:
        if margin[t] is not None:
            sig = np.maximum(margin[t], 0.0) / Z_PI
        else:
            sig = np.abs(pred[t]) * CFG["fallback_sigma"][t] / 100
        out[t] = sig * req.sigma_scale
    return out


def ar1(rng: np.random.Generator, n: int, horizon: int, phi: float) -> np.ndarray:
    """단위분산 정상 AR(1) 경로 n 개: u_0 ~ N(0,1), u_t = φ·u_{t-1} + √(1-φ²)·ε_t."""
    eps = rng.standard_normal((n, horizon))
    u = np.empty_like(eps)
    u[:, 0] = eps[:, 0]
    k = math.sqrt(max(1 - phi**2, 0.0))
    for t in range(1, horizon):
        u[:, t] = phi * u[:, t - 1] + k * eps[:, t]
    return u


def sample(req: ScenarioRequest, base: ScenarioProfile, rng: np.random.Generator) -> dict:
    """오차를 입힌 표본. 키별로 (n_samples, horizon) 배열. raw_demand 는 자르기 전 수요."""
    req = with_defaults(req)
    pred, sig = _arrays(base, "_mw"), sigmas(req, base)
    n, h = req.n_samples, len(pred["demand"])
    phi = {"demand": req.demand_ar, "solar": req.solar_ar, "wind": req.wind_ar}
    raw = {t: pred[t] + sig[t] * ar1(rng, n, h, phi[t]) for t in TARGETS}

    demand = np.maximum(raw["demand"], 0.0)
    solar = np.clip(raw["solar"], 0.0, req.solar_capacity_mw)
    wind = np.clip(raw["wind"], 0.0, req.wind_capacity_mw)
    return {"demand": demand, "solar": solar, "wind": wind, "net": demand - solar - wind,
            "raw_demand": raw["demand"]}


# ── 축약 ─────────────────────────────────────────────────────────────────────


def distance_matrix(x: np.ndarray) -> np.ndarray:
    """행 사이의 유클리드 거리 (n x n). |a-b|² = |a|² + |b|² - 2a·b."""
    sq = (x**2).sum(axis=1)
    return np.sqrt(np.maximum(sq[:, None] + sq[None, :] - 2 * x @ x.T, 0.0))


def forward_selection(x: np.ndarray, k: int) -> list[tuple[int, float]]:
    """fast forward selection (Heitsch & Römisch 2003). (뽑은 표본 인덱스, 확률) k 개.

    표본 확률은 모두 1/n. 매 단계, 아직 안 뽑힌 표본들이 "가장 가까운 뽑힌 표본"까지 가는 확률
    가중 거리 합(Kantorovich 거리)을 가장 많이 줄이는 표본을 하나 더 뽑는다. 끝나면 안 뽑힌
    표본의 확률을 가장 가까운 뽑힌 표본에 더한다 (최적 재분배).
    """
    n = len(x)
    p = np.full(n, 1.0 / n)
    if k >= n:
        return [(i, float(p[i])) for i in range(n)]

    dist = distance_matrix(x)
    nearest = np.full(n, np.inf)   # 각 표본에서 뽑힌 표본까지의 최소 거리
    left = np.ones(n, dtype=bool)  # 아직 안 뽑힌 표본
    picked: list[int] = []
    for _ in range(k):
        # 후보 u 를 더 뽑았을 때 안 뽑힌 표본들의 가중 거리 합 (u 자신은 거리 0)
        cost = (p * left) @ np.minimum(nearest[:, None], dist)
        cost[~left] = np.inf
        u = int(np.argmin(cost))
        picked.append(u)
        left[u] = False
        nearest = np.minimum(nearest, dist[:, u])

    owner = np.asarray(picked)[dist[:, picked].argmin(axis=1)]
    return [(u, float(p[owner == u].sum())) for u in picked]


def reduce(net: np.ndarray, k: int) -> list[tuple[int, float]]:
    """순부하 경로로 n_samples → k 개 축약."""
    return forward_selection(net, k)


# ── 라벨·검증 ────────────────────────────────────────────────────────────────


def _dev(values: np.ndarray, base: np.ndarray) -> float:
    total = base.sum()
    return float((values.sum() - total) / total * 100) if total else 0.0


def label(demand_dev: float, solar_dev: float, wind_dev: float) -> str:
    """편차로 사람이 읽을 이름을 붙인다."""
    parts = []
    if demand_dev >= CFG["label_demand"]:
        parts.append("고수요")
    elif demand_dev <= -CFG["label_demand"]:
        parts.append("저수요")
    if solar_dev <= -CFG["label_solar"]:
        parts.append("흐림")
    elif solar_dev >= CFG["label_solar"]:
        parts.append("맑음")
    if wind_dev >= CFG["label_wind"]:
        parts.append("강풍")
    elif wind_dev <= -CFG["label_wind"]:
        parts.append("약풍")
    return "·".join(parts) or "기준 근접"


def expected_coverage(sigma_scale: float) -> float:
    """σ 를 sigma_scale 배 했을 때 표본이 원래 구간(±z·σ) 안에 들 확률 (%) = 2Φ(z/scale) - 1."""
    return (2 * NormalDist().cdf(Z_PI / sigma_scale) - 1) * 100


def checks(req: ScenarioRequest, base: ScenarioProfile, s: dict,
           scenarios: list[ScenarioDetail]) -> list[ScenarioCheck]:
    """생성 결과가 설계 의도대로인지 확인한다 (formulation.md 7.6)."""
    req = with_defaults(req)
    pred, margin = _arrays(base, "_mw"), _arrays(base, "_margin_mw")
    prob = np.array([sc.probability for sc in scenarios])
    red_net = np.array([
        np.asarray(sc.demand_mw) - np.asarray(sc.solar_mw) - np.asarray(sc.wind_mw)
        for sc in scenarios
    ])

    bias = abs(_dev(s["demand"].mean(axis=0), pred["demand"]))
    raw_net_energy = s["net"].sum(axis=1).mean()
    red_net_energy = float((prob * red_net.sum(axis=1)).sum())
    keep = abs(red_net_energy - raw_net_energy) / raw_net_energy * 100
    upper, lower = CFG["check_tail_upper"], CFG["check_tail_lower"]
    min_n, bias_max, keep_max = (
        CFG["check_min_scenarios"], CFG["check_demand_bias"], CFG["check_net_energy_keep"]
    )
    p90_peak = float(np.percentile(s["net"].max(axis=1), upper))
    top_peak = float(red_net.max())
    p10_low = float(np.percentile(s["net"].min(axis=1), lower))
    bottom_low = float(red_net.min())
    range_ok = bool(
        (s["demand"] >= 0).all()
        and (s["solar"] >= 0).all() and (s["solar"] <= req.solar_capacity_mw).all()
        and (s["wind"] >= 0).all() and (s["wind"] <= req.wind_capacity_mw).all()
    )
    # 표본 평균 vs 예측. 0 에서 자르면 위로 치우치고 (예측이 σ 보다 작은 시각), 태양광·풍력은
    # σ 가 커서 표본 오차도 크다. 판정 없이 크기만 보고한다
    energy_dev = {t: _dev(s[t].mean(axis=0), pred[t]) for t in ("solar", "wind")}

    out = [
        ScenarioCheck(name=f"시나리오 수 ≥ {min_n}", ok=len(scenarios) >= min_n,
                      detail=f"{len(scenarios)}개"),
        ScenarioCheck(name="확률 합 = 1", ok=abs(prob.sum() - 1) < CFG["check_prob_sum_tol"],
                      detail=f"{prob.sum():.6f}"),
        ScenarioCheck(name=f"수요 표본 편향 ≤ {bias_max:g}%", ok=bias <= bias_max,
                      detail=f"표본 평균 vs 기준 예측 {bias:.2f}%"),
    ]
    if margin["demand"] is not None:
        inside = np.abs(s["raw_demand"] - pred["demand"]) <= margin["demand"]
        hit, target = float(inside.mean() * 100), expected_coverage(req.sigma_scale)
        tol = CFG["check_pi_coverage_tol"]
        out.append(ScenarioCheck(
            name=f"수요 표본의 예측구간 적중률 ≈ {target:.0f}%", ok=abs(hit - target) <= tol,
            detail=f"표본 x 시간 중 forecast_api 90% 구간 안 {hit:.1f}% (허용 ±{tol:g}%p)",
        ))
    out += [
        ScenarioCheck(name=f"축약 후 기대 순부하 보존 ≤ {keep_max:g}%", ok=keep <= keep_max,
                      detail=f"축약 전후 일간 순부하 기대값 차이 {keep:.2f}%"),
        ScenarioCheck(name="상방 꼬리 보존", ok=top_peak >= p90_peak,
                      detail=f"최대 피크 {top_peak:,.0f} MW vs 표본 P{upper:g} {p90_peak:,.0f} MW"),
        ScenarioCheck(
            name="하방 꼬리 보존", ok=bottom_low <= p10_low,
            detail=f"최저 순부하 {bottom_low:,.0f} MW vs 표본 P{lower:g} {p10_low:,.0f} MW",
        ),
        ScenarioCheck(
            name="물리 범위", ok=range_ok,
            detail=(f"수요 ≥ 0, 태양광·풍력 0 ~ 설비용량. 표본 평균 일간 에너지 vs 예측 "
                    f"태양광 {energy_dev['solar']:+.1f}%, 풍력 {energy_dev['wind']:+.1f}%"),
        ),
    ]
    return out


# ── 진입점 ───────────────────────────────────────────────────────────────────


def generate(req: ScenarioRequest) -> ScenarioSetResponse:
    """표본 생성 → 축약 → 라벨·검증. 같은 seed 면 같은 결과."""
    req = with_defaults(req)
    base = req.base or DEFAULT_BASE
    rng = np.random.default_rng(req.seed)
    s = sample(req, base, rng)
    picks = reduce(s["net"], min(req.n_scenarios, req.n_samples))

    d0, s0, w0 = (np.asarray(x, dtype=float) for x in (base.demand_mw, base.solar_mw, base.wind_mw))
    raw = []
    for idx, prob in picks:
        dd, sd, wd = _dev(s["demand"][idx], d0), _dev(s["solar"][idx], s0), _dev(s["wind"][idx], w0)
        raw.append(dict(
            probability=prob,
            demand_mw=s["demand"][idx].round(1).tolist(),
            solar_mw=s["solar"][idx].round(1).tolist(),
            wind_mw=s["wind"][idx].round(1).tolist(),
            label=label(dd, sd, wd),
            peak_net_load_mw=float(s["net"][idx].max()),
            demand_dev_pct=dd, solar_dev_pct=sd, wind_dev_pct=wd,
        ))
    raw.sort(key=lambda r: r["peak_net_load_mw"], reverse=True)
    scenarios = [ScenarioDetail(scenario_id=f"S{i + 1:02d}", **r) for i, r in enumerate(raw)]

    def band(x: np.ndarray) -> Band:
        p = np.percentile(x, CFG["band_percentiles"], axis=0).round(1)
        return Band(p5=p[0].tolist(), p50=p[1].tolist(), p95=p[2].tolist())

    return ScenarioSetResponse(
        horizon_h=len(d0), n_samples=req.n_samples, seed=req.seed, base=base,
        scenarios=scenarios, net_load_band=band(s["net"]), demand_band=band(s["demand"]),
        checks=checks(req, base, s, scenarios),
    )
