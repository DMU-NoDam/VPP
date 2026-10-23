"""불확실성 시나리오 생성기 (docs/formulation.md 7절).

기준 예측(수요·태양광·풍력 24h)에 오차를 입혀 표본을 n_samples 개 뽑고,
순부하 경로가 비슷한 것끼리 k-medoids 로 묶어 n_scenarios 개로 줄인다.
묶음 크기 / 전체 = 그 대표 시나리오의 확률.

오차 모델 (파라메트릭, 1년치 잔차가 쌓이면 잔차 부트스트랩으로 교체):
    수요    곱셈 오차, AR(1) — 시간대끼리 같이 움직인다
    태양광  하루 공통 흐림 계수(하방으로 치우친 감마) x 시간 노이즈, 0 ~ 설비용량
    풍력    로그정규 곱셈 오차, AR(1), 0 ~ 설비용량
"""

from __future__ import annotations

import math

import numpy as np

from shared.schemas import (
    Band,
    ScenarioCheck,
    ScenarioDetail,
    ScenarioProfile,
    ScenarioRequest,
    ScenarioSetResponse,
)

# forecast_api 연동 전 기본 예측 (전형적인 한국 하루 부하 패턴을 본뜬 값, MW)
DEFAULT_BASE = ScenarioProfile(
    demand_mw=[
        52000, 50000, 49000, 48000, 49000, 52000, 58000, 66000, 72000, 76000, 79000, 81000,
        82000, 80000, 78000, 77000, 78000, 80000, 84000, 86000, 83000, 74000, 65000, 57000,
    ],
    solar_mw=[
        0, 0, 0, 0, 0, 200, 1200, 3600, 6400, 8800, 10400, 11200,
        11600, 11200, 10000, 8000, 5600, 2800, 800, 100, 0, 0, 0, 0,
    ],
    wind_mw=[round(800 + 700 * math.sin(h / 24 * 4 * math.pi), 1) for h in range(24)],
)

SOLAR_HOURLY_AR, SOLAR_HOURLY_SIGMA = 0.7, 0.08
WIND_AR = 0.85
CLOUD_SHAPE = 0.8  # 감마 형상. 작을수록 가끔 크게 흐린 날이 나온다


# ── 표본 ─────────────────────────────────────────────────────────────────────


def ar1(rng: np.random.Generator, n: int, horizon: int, phi: float, sigma: float) -> np.ndarray:
    """정상 AR(1) 경로 n 개. 각 시점의 표준편차가 sigma 로 일정하다."""
    z = rng.standard_normal((n, horizon))
    e = np.empty_like(z)
    e[:, 0] = sigma * z[:, 0]
    k = sigma * math.sqrt(max(1 - phi**2, 0.0))
    for t in range(1, horizon):
        e[:, t] = phi * e[:, t - 1] + k * z[:, t]
    return e


def sample(req: ScenarioRequest, base: ScenarioProfile, rng: np.random.Generator) -> dict:
    """오차를 입힌 표본. 키별로 (n_samples, horizon) 배열."""
    n = req.n_samples
    d0, s0, w0 = (np.asarray(x, dtype=float) for x in (base.demand_mw, base.solar_mw, base.wind_mw))
    h = len(d0)

    demand = d0 * (1 + ar1(rng, n, h, req.demand_ar, req.demand_sigma_pct / 100))

    m = req.solar_cloud_pct / 100
    cloud = 1 + m - rng.gamma(CLOUD_SHAPE, m / CLOUD_SHAPE, size=n) if m > 0 else np.ones(n)
    hourly = 1 + ar1(rng, n, h, SOLAR_HOURLY_AR, SOLAR_HOURLY_SIGMA)
    solar = np.clip(s0 * cloud[:, None] * hourly, 0, req.solar_capacity_mw)

    sig = req.wind_sigma_pct / 100
    wind = np.clip(w0 * np.exp(ar1(rng, n, h, WIND_AR, sig) - sig**2 / 2), 0, req.wind_capacity_mw)

    return {"demand": demand, "solar": solar, "wind": wind, "net": demand - solar - wind}


# ── 축약 ─────────────────────────────────────────────────────────────────────


def k_medoids(x: np.ndarray, k: int, rng: np.random.Generator, iters: int = 50) -> tuple:
    """(대표 인덱스, 각 표본의 군집 번호). k-means++ 로 시작해 배정·대표 갱신을 반복한다."""
    n = len(x)
    if k >= n:
        return np.arange(n), np.arange(n)

    sq = (x**2).sum(axis=1)  # |a-b|² = |a|² + |b|² - 2a·b — n x n 만 메모리에 둔다
    dist = np.sqrt(np.maximum(sq[:, None] + sq[None, :] - 2 * x @ x.T, 0.0))
    medoids = [int(rng.integers(n))]
    while len(medoids) < k:
        d2 = dist[:, medoids].min(axis=1) ** 2
        medoids.append(int(rng.choice(n, p=d2 / d2.sum())))
    medoids = np.array(medoids)

    for _ in range(iters):
        assign = dist[:, medoids].argmin(axis=1)
        new = medoids.copy()
        for c in range(k):
            members = np.flatnonzero(assign == c)
            if len(members):
                new[c] = members[dist[np.ix_(members, members)].sum(axis=1).argmin()]
        if np.array_equal(new, medoids):
            break
        medoids = new
    return medoids, dist[:, medoids].argmin(axis=1)


TAIL_PCT = 10  # 꼬리 군집 크기 (표본의 %)


def reduce(net: np.ndarray, k: int, rng: np.random.Generator) -> list[tuple[int, float]]:
    """(대표 표본 인덱스, 확률) k 개.

    k-medoids 만 쓰면 대표가 중앙으로 끌려가 꼬리가 사라진다 (VSS 가 작아진다).
    그래서 상방 꼬리(피크 상위 10%)와 하방 꼬리(최저 순부하 하위 10%, 과발전 위험)를
    각각 한 군집으로 떼어 대표 1개씩 두고, 나머지만 k-2 개로 묶는다.
    """
    n = len(net)
    if k < 3 or k >= n:
        medoids, assign = k_medoids(net, k, rng)
        return [(int(m), float((assign == c).sum() / n)) for c, m in enumerate(medoids)]

    peak, low = net.max(axis=1), net.min(axis=1)
    up = peak >= np.percentile(peak, 100 - TAIL_PCT)
    down = ~up & (low <= np.percentile(low, TAIL_PCT))
    groups = [np.flatnonzero(up), np.flatnonzero(down)]

    out = []
    for g in groups:
        local, _ = k_medoids(net[g], 1, rng)
        out.append((int(g[local[0]]), len(g) / n))

    rest = np.flatnonzero(~up & ~down)
    medoids, assign = k_medoids(net[rest], k - 2, rng)
    out += [(int(rest[m]), float((assign == c).sum() / n)) for c, m in enumerate(medoids)]
    return out


# ── 라벨·검증 ────────────────────────────────────────────────────────────────


def _dev(values: np.ndarray, base: np.ndarray) -> float:
    total = base.sum()
    return float((values.sum() - total) / total * 100) if total else 0.0


def label(demand_dev: float, solar_dev: float, wind_dev: float) -> str:
    """편차로 사람이 읽을 이름을 붙인다."""
    parts = []
    if demand_dev >= 1.5:
        parts.append("고수요")
    elif demand_dev <= -1.5:
        parts.append("저수요")
    if solar_dev <= -15:
        parts.append("흐림")
    elif solar_dev >= 5:
        parts.append("맑음")
    if wind_dev >= 20:
        parts.append("강풍")
    elif wind_dev <= -20:
        parts.append("약풍")
    return "·".join(parts) or "기준 근접"


def checks(req: ScenarioRequest, base: ScenarioProfile, s: dict,
           scenarios: list[ScenarioDetail]) -> list[ScenarioCheck]:
    """생성 결과가 설계 의도대로인지 확인한다 (formulation.md 7절 검증 기준)."""
    d0 = np.asarray(base.demand_mw, dtype=float)
    s0 = np.asarray(base.solar_mw, dtype=float)
    prob = np.array([sc.probability for sc in scenarios])
    red_net = np.array([
        np.asarray(sc.demand_mw) - np.asarray(sc.solar_mw) - np.asarray(sc.wind_mw)
        for sc in scenarios
    ])

    bias = abs(_dev(s["demand"].mean(axis=0), d0))
    raw_net_energy = s["net"].sum(axis=1).mean()
    red_net_energy = float((prob * red_net.sum(axis=1)).sum())
    keep = abs(red_net_energy - raw_net_energy) / raw_net_energy * 100
    p90_peak = float(np.percentile(s["net"].max(axis=1), 90))
    top_peak = float(red_net.max())
    p10_low = float(np.percentile(s["net"].min(axis=1), 10))
    bottom_low = float(red_net.min())
    night = s0 == 0
    night_ok = bool(np.all(s["solar"][:, night] == 0))
    range_ok = bool(
        (s["solar"] >= 0).all() and (s["solar"] <= req.solar_capacity_mw).all()
        and (s["wind"] >= 0).all() and (s["wind"] <= req.wind_capacity_mw).all()
    )

    return [
        ScenarioCheck(name="시나리오 수 ≥ 10", ok=len(scenarios) >= 10,
                      detail=f"{len(scenarios)}개"),
        ScenarioCheck(name="확률 합 = 1", ok=abs(prob.sum() - 1) < 1e-9,
                      detail=f"{prob.sum():.6f}"),
        ScenarioCheck(name="수요 표본 편향 ≤ 1%", ok=bias <= 1.0,
                      detail=f"표본 평균 vs 기준 예측 {bias:.2f}%"),
        ScenarioCheck(name="축약 후 기대 순부하 보존 ≤ 1%", ok=keep <= 1.0,
                      detail=f"축약 전후 일간 순부하 기대값 차이 {keep:.2f}%"),
        ScenarioCheck(name="상방 꼬리 보존", ok=top_peak >= p90_peak,
                      detail=f"최대 피크 {top_peak:,.0f} MW vs 표본 P90 {p90_peak:,.0f} MW"),
        ScenarioCheck(name="하방 꼬리 보존", ok=bottom_low <= p10_low,
                      detail=f"최저 순부하 {bottom_low:,.0f} MW vs 표본 P10 {p10_low:,.0f} MW"),
        ScenarioCheck(name="태양광 야간 0 · 물리 범위", ok=night_ok and range_ok,
                      detail="야간 0, 0 ~ 설비용량" if night_ok and range_ok else "범위 벗어남"),
    ]


# ── 진입점 ───────────────────────────────────────────────────────────────────


def generate(req: ScenarioRequest) -> ScenarioSetResponse:
    """표본 생성 → 축약 → 라벨·검증. 같은 seed 면 같은 결과."""
    base = req.base or DEFAULT_BASE
    rng = np.random.default_rng(req.seed)
    s = sample(req, base, rng)
    picks = reduce(s["net"], min(req.n_scenarios, req.n_samples), rng)

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
        p = np.percentile(x, [5, 50, 95], axis=0).round(1)
        return Band(p5=p[0].tolist(), p50=p[1].tolist(), p95=p[2].tolist())

    return ScenarioSetResponse(
        horizon_h=len(d0), n_samples=req.n_samples, seed=req.seed, base=base,
        scenarios=scenarios, net_load_band=band(s["net"]), demand_band=band(s["demand"]),
        checks=checks(req, base, s, scenarios),
    )
