"""시나리오 생성기(dispatch_api/scenarios.py) 단위 테스트 + POST /api/v1/scenarios."""

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

from shared.schemas import ScenarioProfile, ScenarioRequest

DISPATCH_DIR = Path(__file__).resolve().parents[1] / "services" / "dispatch_api"


def _load(name: str):
    if str(DISPATCH_DIR) not in sys.path:
        sys.path.insert(0, str(DISPATCH_DIR))  # main.py 가 `import scenarios` 를 평평하게 쓴다
    spec = importlib.util.spec_from_file_location(name, DISPATCH_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


scenarios = _load("scenarios")

# forecast_api 응답 모양의 기준 예측 (구간 반폭 포함). 수요 반폭 ≈ 예측의 8% → σ ≈ 4.9%
H = 24
FORECAST_BASE = ScenarioProfile(
    demand_mw=[60000.0 + 8000 * np.sin(np.pi * h / H) for h in range(H)],
    solar_mw=[0.0] * 6 + [3000.0] * 12 + [0.0] * 6,
    wind_mw=[150.0] * H,
    demand_margin_mw=[4800.0] * H,
    solar_margin_mw=[15.0] * 6 + [900.0] * 12 + [15.0] * 6,
    wind_margin_mw=[250.0] * H,
    source="forecast_api",
)


@pytest.fixture(autouse=True)
def _no_forecast_api(monkeypatch):
    """forecast_api 가 없는 환경: 닫힌 포트로 바로 실패시켜 base_profile 폴백을 쓴다."""
    monkeypatch.setenv("FORECAST_URL", "http://127.0.0.1:9")


@pytest.fixture(scope="module")
def result():
    return scenarios.generate(ScenarioRequest(base=FORECAST_BASE))


def test_default_set_shape(result) -> None:
    assert result.horizon_h == 24
    assert len(result.scenarios) == 10
    assert [s.scenario_id for s in result.scenarios][:2] == ["S01", "S02"]
    for sc in result.scenarios:
        assert len(sc.demand_mw) == len(sc.solar_mw) == len(sc.wind_mw) == 24
    assert result.base.demand_margin_mw == FORECAST_BASE.demand_margin_mw  # 응답에 구간도 실린다


def test_probabilities_sum_to_one(result) -> None:
    assert sum(s.probability for s in result.scenarios) == pytest.approx(1.0)
    assert all(s.probability > 0 for s in result.scenarios)


def test_sorted_by_peak_net_load(result) -> None:
    peaks = [s.peak_net_load_mw for s in result.scenarios]
    assert peaks == sorted(peaks, reverse=True)


def test_same_seed_same_result() -> None:
    a = scenarios.generate(ScenarioRequest(seed=7))
    b = scenarios.generate(ScenarioRequest(seed=7))
    c = scenarios.generate(ScenarioRequest(seed=8))
    assert a == b
    assert a != c


# forward selection 은 꼬리를 일부러 남기지 않아 꼬리 보존 검사는 실패할 수 있다 (config 주석)
TAIL_CHECKS = {"상방 꼬리 보존", "하방 꼬리 보존"}


@pytest.mark.parametrize("seed", range(20))
@pytest.mark.parametrize("base", [FORECAST_BASE, None], ids=["forecast", "config"])
def test_design_checks_pass(seed: int, base) -> None:
    res = scenarios.generate(ScenarioRequest(seed=seed, base=base))
    failed = [c.name for c in res.checks if not c.ok and c.name not in TAIL_CHECKS]
    assert failed == []


def test_coverage_check_only_with_margins() -> None:
    names = [c.name for c in scenarios.generate(ScenarioRequest()).checks]
    assert not any("적중률" in n for n in names)  # 폴백 기준 예측에는 구간이 없다
    names = [c.name for c in scenarios.generate(ScenarioRequest(base=FORECAST_BASE)).checks]
    assert any("적중률" in n for n in names)


def test_bands_are_ordered(result) -> None:
    for band in (result.net_load_band, result.demand_band):
        assert all(lo <= mid <= hi for lo, mid, hi in zip(band.p5, band.p50, band.p95))


# ── σ ────────────────────────────────────────────────────────────────────────


def test_sigma_from_margin_and_fallback() -> None:
    sig = scenarios.sigmas(ScenarioRequest(), FORECAST_BASE)
    assert sig["demand"] == pytest.approx(np.full(H, 4800.0 / 1.6449), rel=1e-3)
    assert scenarios.Z_PI == pytest.approx(1.6449, abs=1e-4)
    doubled = scenarios.sigmas(ScenarioRequest(sigma_scale=2.0), FORECAST_BASE)
    assert doubled["wind"] == pytest.approx(2 * sig["wind"])

    base = scenarios.DEFAULT_BASE
    fb = scenarios.sigmas(ScenarioRequest(), base)
    pct = scenarios.CFG["fallback_sigma"]["demand"] / 100
    assert fb["demand"] == pytest.approx(np.asarray(base.demand_mw) * pct)


def test_sample_hits_forecast_interval() -> None:
    """표본의 시간별 90% 범위가 forecast_api 구간과 같다 (σ = 반폭 / z 의 의미)."""
    req = ScenarioRequest(n_samples=2000)
    s = scenarios.sample(req, FORECAST_BASE, np.random.default_rng(0))
    err = s["raw_demand"] - np.asarray(FORECAST_BASE.demand_mw)
    assert (np.abs(err) <= 4800.0).mean() == pytest.approx(0.90, abs=0.015)
    assert scenarios.expected_coverage(1.0) == pytest.approx(90.0)
    assert scenarios.expected_coverage(2.0) < 60


def test_ar1_is_unit_variance_with_target_correlation() -> None:
    u = scenarios.ar1(np.random.default_rng(0), 20000, 24, 0.9)
    assert u.std(axis=0).mean() == pytest.approx(1.0, rel=0.03)
    assert np.corrcoef(u[:, 10], u[:, 11])[0, 1] == pytest.approx(0.9, abs=0.02)


def test_sample_respects_physical_limits() -> None:
    req = ScenarioRequest(sigma_scale=3.0, wind_capacity_mw=200)
    s = scenarios.sample(req, FORECAST_BASE, np.random.default_rng(0))
    req = scenarios.with_defaults(req)
    assert (s["demand"] >= 0).all()
    assert (s["solar"] >= 0).all() and (s["solar"] <= req.solar_capacity_mw).all()
    assert (s["wind"] >= 0).all() and (s["wind"] <= 200).all()


def test_custom_base_profile_without_margins() -> None:
    base = ScenarioProfile(demand_mw=[100.0] * 12, solar_mw=[0.0] * 6 + [10.0] * 6,
                           wind_mw=[5.0] * 12)
    res = scenarios.generate(ScenarioRequest(base=base, n_samples=100))
    assert res.horizon_h == 12
    for sc in res.scenarios:
        assert sc.solar_mw[:6] == [0.0] * 6  # 예측 0 이면 fallback σ 도 0


# ── 축약 (forward selection) ─────────────────────────────────────────────────


def test_forward_selection_probabilities_and_size() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(0, 1, size=(200, 24))
    picks = scenarios.forward_selection(x, 10)
    assert len(picks) == 10 and len({i for i, _ in picks}) == 10
    assert sum(p for _, p in picks) == pytest.approx(1.0)
    assert scenarios.forward_selection(x[:5], 9) == [(i, pytest.approx(0.2)) for i in range(5)]


def test_forward_selection_first_pick_is_medoid() -> None:
    """첫 번째로 뽑는 표본은 다른 표본까지의 거리 합이 가장 작은 표본이다 (Heitsch & Römisch)."""
    rng = np.random.default_rng(1)
    x = rng.normal(size=(50, 4))
    dist = scenarios.distance_matrix(x)
    assert scenarios.forward_selection(x, 1)[0] == (int(dist.sum(axis=0).argmin()), 1.0)


def test_forward_selection_redistributes_to_nearest() -> None:
    """두 덩어리면 각 덩어리에서 하나씩 뽑고, 덩어리 크기가 확률이 된다.

    첫 번째는 전체 거리 합이 가장 작은 2 (합 200), 두 번째는 100·101 이 같은 값이라 앞의 것.
    """
    x = np.array([[0.0], [1.0], [2.0], [100.0], [101.0]])
    picks = dict(scenarios.forward_selection(x, 2))
    assert picks == {2: pytest.approx(0.6), 3: pytest.approx(0.4)}


@pytest.mark.parametrize(
    ("dev", "expected"),
    [
        ((2.0, 0.0, 0.0), "고수요"),
        ((-2.0, -20.0, 0.0), "저수요·흐림"),
        ((0.0, 16.0, 25.0), "맑음·강풍"),
        ((0.0, 0.0, -25.0), "약풍"),
        ((0.5, -5.0, 5.0), "기준 근접"),
    ],
)
def test_label(dev: tuple, expected: str) -> None:
    assert scenarios.label(*dev) == expected


def test_endpoint() -> None:
    from fastapi.testclient import TestClient

    app = _load("main").app
    client = TestClient(app)
    res = client.post("/api/v1/scenarios", json={"seed": 3, "n_scenarios": 12})
    assert res.status_code == 200
    body = res.json()
    assert len(body["scenarios"]) == 12
    assert body["base"]["source"] == "config"  # forecast_api 가 없으면 폴백
    assert all(c["ok"] for c in body["checks"] if c["name"] not in TAIL_CHECKS)

    assert client.post("/api/v1/scenarios", json={"n_samples": 10}).status_code == 422
    assert client.post("/api/v1/scenarios", json={"demand_ar": 1.0}).status_code == 422


def test_empty_request_uses_config_defaults() -> None:
    """비운 필드는 config/stochastic.yaml 의 scenario_generator 값으로 채운다."""
    req = scenarios.with_defaults(ScenarioRequest(seed=0))
    assert req.seed == 0  # 준 값은 그대로 (0 도)
    for field in scenarios.REQUEST_FIELDS:
        if field != "seed":
            assert getattr(req, field) == scenarios.CFG[field]
