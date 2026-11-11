"""forecast_api 연동(dispatch_api/forecast_client.py) 단위 테스트. HTTP 는 가짜로 바꾼다."""

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

DISPATCH_DIR = Path(__file__).resolve().parents[1] / "services" / "dispatch_api"


def _load(name: str):
    if str(DISPATCH_DIR) not in sys.path:
        sys.path.insert(0, str(DISPATCH_DIR))
    spec = importlib.util.spec_from_file_location(name, DISPATCH_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fc = _load("forecast_client")
CFG = fc.ForecastConfig(history_days=365, horizon_h=24, lead_h=13, timeout_s=1.0)
VALUES = {"demand": 50000.0, "solar": -2.0, "wind": 300.0}
MARGIN = {"demand": 4000.0, "solar": 500.0, "wind": 200.0}


def fake_forecast_api(
    data_end: dict[str, datetime], seen: list | None = None, n: int | None = None
):
    """forecast_api 흉내: target 마다 min(end_date, 데이터 끝) + 1h 부터 horizon_hours 시간."""

    def post(payload, timeout_s):
        if seen is not None:
            seen.append(payload)
        end = datetime.fromisoformat(payload["end_date"])
        out = {}
        for t in fc.TARGETS:
            first = min(end, data_end[t]) + timedelta(hours=1)
            out[t] = {"points": [
                {"datetime": (first + timedelta(hours=h)).isoformat(), "predicted": VALUES[t],
                 "lower_bound": max(VALUES[t] - MARGIN[t], 0.0),
                 "upper_bound": VALUES[t] + MARGIN[t]}
                for h in range(n or payload["horizon_hours"])
            ]}
        return {"forecasts": out}

    return post


def test_day_ahead_window_from_latest_common_data(monkeypatch) -> None:
    # 수요는 11/2 오전까지, 태양광·풍력은 9/27 23시까지 데이터가 있는 상황
    ends = {"demand": datetime(2026, 11, 2, 9), "solar": datetime(2026, 9, 27, 23),
            "wind": datetime(2026, 9, 27, 23)}
    seen = []
    monkeypatch.setattr(fc, "_post", fake_forecast_api(ends, seen))
    base = fc.latest_base(datetime(2026, 11, 3, 10, 30, tzinfo=fc.KST), CFG)

    assert [p["end_date"] for p in seen] == ["2026-11-03 10:00:00", "2026-09-27 11:00:00"]
    assert seen[-1]["horizon_hours"] == 36 and seen[-1]["start_date"] == "2025-09-27"
    assert base.issued_at == datetime(2026, 9, 27, 11, tzinfo=fc.KST)  # 전날 11시 데이터로
    assert base.start == datetime(2026, 9, 28, 0, tzinfo=fc.KST)       # 13~36시간 뒤 = 9/28 하루
    assert base.source == "forecast_api" and len(base.demand_mw) == 24
    assert base.demand_margin_mw == [4000.0] * 24
    assert base.solar_margin_mw == [500.0] * 24  # lower 가 0 에서 잘려도 반폭은 upper - 예측
    assert min(base.solar_mw) == 0.0             # 음수 예측은 0


def test_decision_time_rolls_back_a_day() -> None:
    assert fc.decision_time(datetime(2026, 9, 27, 23), 13) == datetime(2026, 9, 27, 11)
    assert fc.decision_time(datetime(2026, 9, 27, 11), 13) == datetime(2026, 9, 27, 11)
    assert fc.decision_time(datetime(2026, 9, 27, 10), 13) == datetime(2026, 9, 26, 11)
    assert fc.decision_time(datetime(2026, 9, 27, 5), 24) == datetime(2026, 9, 27, 0)


def test_misaligned_or_short_raises(monkeypatch) -> None:
    ends = {t: datetime(2026, 9, 27, 23) for t in fc.TARGETS}
    now = datetime(2026, 9, 28, 12, tzinfo=fc.KST)
    monkeypatch.setattr(fc, "_post", fake_forecast_api(ends, n=10))  # 36시간보다 짧다
    with pytest.raises(fc.ForecastUnavailableError):
        fc.latest_base(now, CFG)

    def drifting(payload, timeout_s):  # 다시 불러도 태양광만 하루 늦게 시작한다
        body = fake_forecast_api(ends)(payload, timeout_s)
        for p in body["forecasts"]["solar"]["points"]:
            p["datetime"] = (datetime.fromisoformat(p["datetime"]) + timedelta(days=1)).isoformat()
        return body

    monkeypatch.setattr(fc, "_post", drifting)
    with pytest.raises(fc.ForecastUnavailableError):
        fc.latest_base(now, CFG)
    monkeypatch.setattr(fc, "_post", lambda p, t: {"forecasts": {}})
    with pytest.raises(fc.ForecastUnavailableError):
        fc.latest_base(now, CFG)


def test_resolve_base_falls_back_to_config(monkeypatch) -> None:
    monkeypatch.setenv("FORECAST_URL", "http://127.0.0.1:9")  # 닫힌 포트
    base = fc.resolve_base(datetime(2026, 11, 2, tzinfo=fc.KST))
    assert base.source == "config" and base.start is None and base.demand_margin_mw is None
    assert base.demand_mw == fc.DEFAULT_BASE.demand_mw


def test_repo_config() -> None:
    cfg = fc.load_config()
    assert cfg.horizon_h == 24 and cfg.lead_h == 13 and cfg.steps == 36
    assert cfg.history_days >= 365 and cfg.timeout_s > 0
