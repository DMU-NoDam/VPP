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


@pytest.fixture(scope="module")
def result():
    return scenarios.generate(ScenarioRequest())


def test_default_set_shape(result) -> None:
    assert result.horizon_h == 24
    assert len(result.scenarios) == 10
    assert [s.scenario_id for s in result.scenarios][:2] == ["S01", "S02"]
    for sc in result.scenarios:
        assert len(sc.demand_mw) == len(sc.solar_mw) == len(sc.wind_mw) == 24


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


@pytest.mark.parametrize("seed", range(20))
@pytest.mark.parametrize("k", [10, 15])
def test_all_checks_pass(seed: int, k: int) -> None:
    res = scenarios.generate(ScenarioRequest(seed=seed, n_scenarios=k))
    failed = [c.name for c in res.checks if not c.ok]
    assert failed == []


def test_bands_are_ordered(result) -> None:
    for band in (result.net_load_band, result.demand_band):
        assert all(lo <= mid <= hi for lo, mid, hi in zip(band.p5, band.p50, band.p95))


def test_custom_base_profile() -> None:
    base = ScenarioProfile(demand_mw=[100.0] * 12, solar_mw=[0.0] * 6 + [10.0] * 6,
                           wind_mw=[5.0] * 12)
    res = scenarios.generate(ScenarioRequest(base=base, n_samples=100))
    assert res.horizon_h == 12
    for sc in res.scenarios:
        assert sc.solar_mw[:6] == [0.0] * 6


def test_sample_respects_physical_limits() -> None:
    req = ScenarioRequest(solar_cloud_pct=40, wind_sigma_pct=60, wind_capacity_mw=1500)
    s = scenarios.sample(req, scenarios.DEFAULT_BASE, np.random.default_rng(0))
    assert (s["solar"] >= 0).all() and (s["solar"] <= req.solar_capacity_mw).all()
    assert (s["wind"] >= 0).all() and (s["wind"] <= 1500).all()


def test_ar1_has_target_sigma() -> None:
    e = scenarios.ar1(np.random.default_rng(0), 20000, 24, 0.9, 0.05)
    assert e.std(axis=0).mean() == pytest.approx(0.05, rel=0.05)


def test_reduce_keeps_tails() -> None:
    rng = np.random.default_rng(0)
    net = rng.normal(0, 1, size=(200, 24))
    picks = scenarios.reduce(net, 10, rng)
    assert len(picks) == 10
    assert sum(p for _, p in picks) == pytest.approx(1.0)
    chosen_peaks = net[[i for i, _ in picks]].max(axis=1)
    assert chosen_peaks.max() >= np.percentile(net.max(axis=1), 90)


def test_reduce_small_k_and_k_over_n() -> None:
    rng = np.random.default_rng(0)
    net = rng.normal(size=(5, 3))
    assert len(scenarios.reduce(net, 2, rng)) == 2
    assert len(scenarios.reduce(net, 9, rng)) == 5


@pytest.mark.parametrize(
    ("dev", "expected"),
    [
        ((2.0, 0.0, 0.0), "고수요"),
        ((-2.0, -20.0, 0.0), "저수요·흐림"),
        ((0.0, 6.0, 25.0), "맑음·강풍"),
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
    assert all(c["ok"] for c in body["checks"])

    assert client.post("/api/v1/scenarios", json={"n_samples": 10}).status_code == 422
