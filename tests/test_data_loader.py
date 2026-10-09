"""data_loader 단위 테스트.

중점
  1. 5분 -> 1시간 집계가 시간평균으로 정확히 되는지
  2. timestamp 가 tz-aware(Asia/Seoul) 인지
  3. 불완전한 시간이 버려지는지
  4. 순수요 계산식이 모드별로 맞는지 + 이중 차감이 잡히는지
"""

from __future__ import annotations

import pandas as pd
import pytest
import requests

import loaders.data_loader as dl
from loaders.data_loader import (
    DataError,
    build_net_demand,
    read_csv_dataset,
    to_hourly,
    wide_generation,
)
from models import Generator, Settings


def demand_rows(start: str, hours: int, value_per_hour: list[float], step_min: int = 5):
    """시간마다 일정한 값을 갖는 5분 간격 행들. 시간평균 검증이 쉬워진다."""
    rows = []
    ts = pd.Timestamp(start)
    per_hour = 60 // step_min
    for h in range(hours):
        for i in range(per_hour):
            rows.append({
                "ts": (ts + pd.Timedelta(hours=h, minutes=i * step_min)
                       ).strftime("%Y-%m-%d %H:%M:%S"),
                "demand_mw": value_per_hour[h],
            })
    return rows


# ── 리샘플 ───────────────────────────────────────────────────────────────────


def test_hourly_mean_of_five_minute_data():
    """12개 샘플의 평균이 그 시간의 값이 된다."""
    rows = demand_rows("2026-09-20 00:00:00", 2, [1000.0, 2000.0])

    hourly = to_hourly(rows, "ts", ["demand_mw"], resample="mean")

    assert len(hourly) == 2
    assert hourly["demand_mw"].tolist() == [1000.0, 2000.0]


def test_hourly_mean_actually_averages():
    rows = [
        {"ts": "2026-09-20 00:00:00", "demand_mw": 100.0},
        {"ts": "2026-09-20 00:30:00", "demand_mw": 200.0},
    ]
    hourly = to_hourly(rows, "ts", ["demand_mw"], resample="mean",
                       require_complete=False)

    assert hourly["demand_mw"].iloc[0] == pytest.approx(150.0)


def test_instant_takes_first_sample():
    rows = [
        {"ts": "2026-09-20 00:00:00", "demand_mw": 100.0},
        {"ts": "2026-09-20 00:30:00", "demand_mw": 200.0},
    ]
    hourly = to_hourly(rows, "ts", ["demand_mw"], resample="instant",
                       require_complete=False)

    assert hourly["demand_mw"].iloc[0] == pytest.approx(100.0)


def test_incomplete_hour_dropped():
    """5분 간격이면 1시간에 12개가 와야 한다. 모자란 시간은 평균이 왜곡된다."""
    rows = demand_rows("2026-09-20 00:00:00", 1, [1000.0])
    rows += [{"ts": "2026-09-20 01:00:00", "demand_mw": 9999.0}]  # 1개뿐

    hourly = to_hourly(rows, "ts", ["demand_mw"], resample="mean")

    assert len(hourly) == 1
    assert hourly["demand_mw"].tolist() == [1000.0]


def test_incomplete_hour_kept_when_not_required():
    rows = demand_rows("2026-09-20 00:00:00", 1, [1000.0])
    rows += [{"ts": "2026-09-20 01:00:00", "demand_mw": 9999.0}]

    hourly = to_hourly(rows, "ts", ["demand_mw"], require_complete=False)

    assert len(hourly) == 2


# ── 시간대 ───────────────────────────────────────────────────────────────────


def test_timestamps_are_timezone_aware():
    """CLAUDE.md: timestamp 는 tz-aware (Asia/Seoul). 원본은 naive 문자열이다."""
    rows = demand_rows("2026-09-20 00:00:00", 1, [1000.0])

    hourly = to_hourly(rows, "ts", ["demand_mw"])

    assert hourly.index.tz is not None
    assert str(hourly.index.tz) == "Asia/Seoul"
    assert hourly.index[0].utcoffset().total_seconds() == 9 * 3600


# ── 입력 검증 ────────────────────────────────────────────────────────────────


def test_missing_column_rejected():
    with pytest.raises(DataError, match="demand_mw"):
        to_hourly([{"ts": "2026-09-20 00:00:00"}], "ts", ["demand_mw"])


def test_unparseable_timestamp_rejected():
    rows = [{"ts": "2026/09/20 00:00", "demand_mw": 1.0}]

    with pytest.raises(DataError, match="파싱 실패"):
        to_hourly(rows, "ts", ["demand_mw"])


def test_empty_rows_rejected():
    with pytest.raises(DataError):
        to_hourly([], "ts", ["demand_mw"])


# ── 순수요 ───────────────────────────────────────────────────────────────────


def net_demand_frames(demand: float, solar: float, renewable: float):
    index = pd.DatetimeIndex(
        [pd.Timestamp("2026-09-20 00:00:00", tz="Asia/Seoul")], name="ts"
    )
    return (
        pd.DataFrame({"demand_mw": [demand]}, index=index),
        pd.DataFrame({"solar": [solar], "renewable": [renewable]}, index=index),
    )


def test_net_demand_subtracts_must_take():
    demand, generation = net_demand_frames(70000, 5000, 4000)

    net = build_net_demand(demand, generation, "subtract_solar_renewable")

    assert net.iloc[0] == pytest.approx(61000.0)


def test_net_demand_renewable_only():
    demand, generation = net_demand_frames(70000, 5000, 4000)

    net = build_net_demand(demand, generation, "subtract_renewable_only")

    assert net.iloc[0] == pytest.approx(66000.0)


def test_net_demand_demand_only():
    demand, generation = net_demand_frames(70000, 5000, 4000)

    net = build_net_demand(demand, generation, "demand_only")

    assert net.iloc[0] == pytest.approx(70000.0)


def test_negative_net_demand_rejected():
    """이중 차감이면 순수요가 음수로 떨어진다. 조용히 넘기면 수급 균형식이 망가진다."""
    demand, generation = net_demand_frames(5000, 4000, 3000)

    with pytest.raises(DataError, match="음수"):
        build_net_demand(demand, generation, "subtract_solar_renewable")


def test_misaligned_timestamps_rejected():
    demand, generation = net_demand_frames(70000, 5000, 4000)
    generation.index = pd.DatetimeIndex(
        [pd.Timestamp("2026-09-21 00:00:00", tz="Asia/Seoul")], name="ts"
    )

    with pytest.raises(DataError, match="맞지 않는다"):
        build_net_demand(demand, generation, "subtract_solar_renewable")


def test_missing_must_take_fuel_rejected():
    demand, generation = net_demand_frames(70000, 5000, 4000)

    with pytest.raises(DataError, match="renewable"):
        build_net_demand(demand, generation.drop(columns=["renewable"]),
                         "subtract_solar_renewable")


# ── 세로형 -> 가로형 ─────────────────────────────────────────────────────────


def test_wide_generation_pivots_long_format():
    rows = [
        {"ts": "2026-09-20 00:00:00", "fuel": "solar", "mw": 100.0},
        {"ts": "2026-09-20 00:00:00", "fuel": "nuclear", "mw": 20000.0},
    ]

    wide = wide_generation(rows)

    assert len(wide) == 1
    assert wide[0]["solar"] == 100.0
    assert wide[0]["nuclear"] == 20000.0


def test_wide_generation_requires_expected_columns():
    with pytest.raises(DataError, match="fuel"):
        wide_generation([{"ts": "2026-09-20 00:00:00", "mw": 1.0}])


# ── CSV 소스 ─────────────────────────────────────────────────────────────────


def test_read_csv_dataset(csv_dir):
    rows = read_csv_dataset(csv_dir, "fuel_cost")

    assert rows
    assert {"month", "fuel", "cost_won_per_kwh"} <= set(rows[0])


def test_read_csv_missing_file_rejected(tmp_path):
    with pytest.raises(DataError, match="없다"):
        read_csv_dataset(tmp_path, "없는데이터셋")


# ── fetch_dataset (REST) ─────────────────────────────────────────────────────


class _FakeResponse:
    """requests.Response 를 흉내낸다. 네트워크 없이 fetch_dataset 경로를 검증한다."""

    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self) -> dict:
        return self._payload


def test_fetch_dataset_returns_rows(monkeypatch):
    """계약은 docs/api_spec.md: { dataset, count, rows }."""
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return _FakeResponse({
            "dataset": "power_demand", "count": 1,
            "rows": [{"ts": "2026-09-22 09:00:00", "demand_mw": 71612.0}],
        })

    monkeypatch.setattr(dl.requests, "get", fake_get)

    rows = dl.fetch_dataset("http://collector:8000", "power_demand",
                             start="2026-09-22 00:00:00", limit=10)

    assert rows == [{"ts": "2026-09-22 09:00:00", "demand_mw": 71612.0}]
    assert captured["url"] == "http://collector:8000/data/power_demand"
    assert captured["params"] == {"start": "2026-09-22 00:00:00", "limit": 10}


def test_fetch_dataset_strips_trailing_slash_in_base_url(monkeypatch):
    monkeypatch.setattr(
        dl.requests, "get",
        lambda url, params=None, timeout=None: _FakeResponse({"rows": []})
        if url == "http://collector:8000/data/smp" else (_ for _ in ()).throw(
            AssertionError(f"unexpected url {url}")
        ),
    )

    dl.fetch_dataset("http://collector:8000/", "smp")


def test_fetch_dataset_missing_rows_key_rejected(monkeypatch):
    monkeypatch.setattr(
        dl.requests, "get",
        lambda url, params=None, timeout=None: _FakeResponse({"dataset": "smp"}),
    )

    with pytest.raises(DataError, match="rows"):
        dl.fetch_dataset("http://collector:8000", "smp")


def test_fetch_dataset_http_error_propagates(monkeypatch):
    monkeypatch.setattr(
        dl.requests, "get",
        lambda url, params=None, timeout=None: _FakeResponse({}, status_code=404),
    )

    with pytest.raises(requests.HTTPError):
        dl.fetch_dataset("http://collector:8000", "없는이름")


# ── _latest_storage_rate ──────────────────────────────────────────────────────


def test_latest_storage_rate_none_when_no_rows():
    assert dl._latest_storage_rate(None) is None
    assert dl._latest_storage_rate([]) is None


def test_latest_storage_rate_picks_most_recent():
    rows = [
        {"observed_at": "2026-09-19 16:10:00", "dam_code": "1012110",
         "storage_rate_pct": 68.1},
        {"observed_at": "2026-09-19 16:20:00", "dam_code": "1012110",
         "storage_rate_pct": 68.2},
    ]

    assert dl._latest_storage_rate(rows) == pytest.approx(68.2)


def test_latest_storage_rate_averages_multiple_dams():
    """댐이 여러 개면 같은 최신 시각의 값을 평균한다. [미정] 가중 방식은 팀 결정."""
    rows = [
        {"observed_at": "2026-09-19 16:10:00", "dam_code": "A", "storage_rate_pct": 20.0},
        {"observed_at": "2026-09-19 16:10:00", "dam_code": "B", "storage_rate_pct": 40.0},
    ]

    assert dl._latest_storage_rate(rows) == pytest.approx(30.0)


def test_latest_storage_rate_missing_column_rejected():
    with pytest.raises(DataError, match="storage_rate_pct"):
        dl._latest_storage_rate([{"observed_at": "2026-09-19 16:10:00"}])


# ── build_dispatch_input (조립 전체) ──────────────────────────────────────────


def _synthetic_generator(fuel="lng", id_="lng_1", cost=150000.0):
    return Generator(
        id=id_, fuel=fuel, p_max_mw=2000, p_min_mw=800,
        ramp_up_mw_per_h=2000, ramp_down_mw_per_h=2000,
        min_up_h=2, min_down_h=2, startup_cost_krw=4e7,
        co2_ton_per_mwh=0.458, u_init=1, init_state_h=4,
        fuel_cost_krw_per_mwh=cost,
    )


def _synthetic_settings(**overrides):
    base = dict(
        horizon_h=3, time_step_min=60, timezone="Asia/Seoul",
        solver="HiGHS", solver_msg=False, solver_time_limit_s=60,
        solver_mip_gap=0.0, reserve_margin_ratio=0.10, resample="mean",
        net_demand_mode="subtract_solar_renewable", fuel_cost_month="latest",
    )
    base.update(overrides)
    return Settings(**base)


def _synthetic_series(start: str, hours: int, demand, solar, renewable):
    """3시간치 5분 간격 demand/generation 원시 행을 만든다."""
    demand_rows_, gen_rows_ = [], []
    ts0 = pd.Timestamp(start)
    for h in range(hours):
        for i in range(12):  # 5분 x 12 = 1시간, 완전한 시간으로 맞춤
            ts = (ts0 + pd.Timedelta(hours=h, minutes=i * 5)).strftime("%Y-%m-%d %H:%M:%S")
            demand_rows_.append({"ts": ts, "demand_mw": demand[h]})
            gen_rows_.append({"ts": ts, "fuel": "solar", "mw": solar[h]})
            gen_rows_.append({"ts": ts, "fuel": "renewable", "mw": renewable[h]})
    return demand_rows_, gen_rows_


def test_build_dispatch_input_end_to_end():
    """로더 전체 경로: 원시 행 -> DispatchInput. MILP 가 받는 최종 모양을 검증한다."""
    demand_rows_, gen_rows_ = _synthetic_series(
        "2026-09-20 00:00:00", hours=3,
        demand=[70000.0, 72000.0, 68000.0],
        solar=[1000.0, 2000.0, 500.0],
        renewable=[4000.0, 4000.0, 4000.0],
    )
    dam_rows_ = [
        {"observed_at": "2026-09-20 02:00:00", "dam_code": "1012110",
         "storage_rate_pct": 68.1},
    ]
    settings = _synthetic_settings(horizon_h=3)
    generators = [_synthetic_generator()]

    result = dl.build_dispatch_input(
        generators, settings, demand_rows_, gen_rows_, dam_rows_,
    )

    assert result.horizon_h == 3
    assert result.generators == generators
    assert result.reserve_margin_ratio == pytest.approx(0.10)
    assert result.storage_rate_pct == pytest.approx(68.1)
    # 순수요 = 수요 - (solar + renewable), subtract_solar_renewable 기본값
    assert result.net_demand_mw == [
        pytest.approx(70000.0 - 1000.0 - 4000.0),
        pytest.approx(72000.0 - 2000.0 - 4000.0),
        pytest.approx(68000.0 - 500.0 - 4000.0),
    ]
    assert all(ts.tzinfo is not None for ts in result.timestamps)


def test_build_dispatch_input_tail_truncates_to_horizon():
    """1시간 집계가 horizon 보다 많으면 끝(최신)에서 horizon 만큼만 쓴다."""
    demand_rows_, gen_rows_ = _synthetic_series(
        "2026-09-20 00:00:00", hours=5,
        demand=[1.0, 2.0, 3.0, 4.0, 5.0],
        solar=[0.0] * 5, renewable=[0.0] * 5,
    )
    settings = _synthetic_settings(horizon_h=2)

    result = dl.build_dispatch_input(
        [_synthetic_generator()], settings, demand_rows_, gen_rows_,
    )

    assert result.horizon_h == 2
    assert result.net_demand_mw == [4.0, 5.0]


def test_build_dispatch_input_insufficient_history_rejected():
    """원본이 horizon 보다 짧으면 (조용히 패딩하지 않고) 예외를 낸다."""
    demand_rows_, gen_rows_ = _synthetic_series(
        "2026-09-20 00:00:00", hours=2,
        demand=[1.0, 2.0], solar=[0.0, 0.0], renewable=[0.0, 0.0],
    )
    settings = _synthetic_settings(horizon_h=24)

    with pytest.raises(DataError, match="horizon"):
        dl.build_dispatch_input([_synthetic_generator()], settings, demand_rows_, gen_rows_)


def test_build_dispatch_input_without_dam_rows_has_no_storage_rate():
    demand_rows_, gen_rows_ = _synthetic_series(
        "2026-09-20 00:00:00", hours=1,
        demand=[70000.0], solar=[0.0], renewable=[0.0],
    )
    settings = _synthetic_settings(horizon_h=1)

    result = dl.build_dispatch_input(
        [_synthetic_generator()], settings, demand_rows_, gen_rows_, dam_rows=None,
    )

    assert result.storage_rate_pct is None


def test_build_dispatch_input_real_csv_data(csv_dir):
    """레포에 커밋된 실제 CSV 로 전체 경로가 깨지지 않는지 (회귀 방지)."""
    settings = _synthetic_settings(horizon_h=24)
    generators = [_synthetic_generator(fuel="lng")]

    result = dl.build_dispatch_input(
        generators, settings,
        dl.read_csv_dataset(csv_dir, "power_demand"),
        dl.read_csv_dataset(csv_dir, "generation_by_fuel"),
        dl.read_csv_dataset(csv_dir, "dam_status"),
    )

    assert result.horizon_h == 24
    assert all(v > 0 for v in result.net_demand_mw)
    assert result.storage_rate_pct is not None


def test_many_incomplete_hours_logs_truncated(caplog):
    """불완전한 시간이 4개 넘으면 로그에 '...' 가 붙는다 (로그 가독성용 분기).

    5개 시간 모두 샘플이 1개뿐(기대 12개)이라 전부 걸러지고 결과가 비어 예외가
    난다. 이 테스트의 목적은 그 전에 찍히는 로그 문구이므로 예외도 같이 검증한다.
    """
    rows = [{"ts": f"2026-09-20 0{h}:00:00", "demand_mw": 1.0} for h in range(5)]

    with caplog.at_level("INFO"), pytest.raises(DataError, match="비었다"):
        to_hourly(rows, "ts", ["demand_mw"])

    assert "..." in caplog.text
