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
    wide_trading,
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
                       min_sample_ratio=0.0)

    assert hourly["demand_mw"].iloc[0] == pytest.approx(150.0)


def test_instant_takes_first_sample():
    rows = [
        {"ts": "2026-09-20 00:00:00", "demand_mw": 100.0},
        {"ts": "2026-09-20 00:30:00", "demand_mw": 200.0},
    ]
    hourly = to_hourly(rows, "ts", ["demand_mw"], resample="instant",
                       min_sample_ratio=0.0)

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

    hourly = to_hourly(rows, "ts", ["demand_mw"], min_sample_ratio=0.0)

    assert len(hourly) == 2


def test_single_missing_sample_survives_relaxed_ratio():
    """KPX 가 한 시각만 빼먹은 경우(11/12). 0.9 면 살리고 1.0 이면 버린다.

    원본 결측의 26%가 23:55 단발 누락이다. 11개 평균과 12개 평균은 사실상 같으니
    그 때문에 격자에 구멍을 만들 이유가 없다.
    """
    rows = [r for r in demand_rows("2026-09-20 00:00:00", 1, [1000.0])
            if not r["ts"].endswith("00:55:00")]
    assert len(rows) == 11

    assert len(to_hourly(rows, "ts", ["demand_mw"], min_sample_ratio=0.9)) == 1
    with pytest.raises(DataError, match="비었다"):
        to_hourly(rows, "ts", ["demand_mw"], min_sample_ratio=1.0)


def test_long_gap_still_dropped_at_relaxed_ratio():
    """수십 분짜리 연속 누락은 완화해도 걸러야 한다 (예 23:20~23:55)."""
    rows = demand_rows("2026-09-20 00:00:00", 1, [1000.0])[:5]  # 25분치만
    assert len(rows) == 5

    with pytest.raises(DataError, match="비었다"):
        to_hourly(rows, "ts", ["demand_mw"], min_sample_ratio=0.9)


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


def net_demand_frames(demand: float, solar: float, renewable: float, wind: float = 0.0):
    index = pd.DatetimeIndex(
        [pd.Timestamp("2026-09-20 00:00:00", tz="Asia/Seoul")], name="ts"
    )
    return (
        pd.DataFrame({"demand_mw": [demand]}, index=index),
        pd.DataFrame(
            {"solar": [solar], "wind": [wind], "renewable": [renewable]}, index=index
        ),
    )


def test_net_demand_subtracts_wind():
    """wind 는 trading_by_fuel 에서 분리된 발전원이라 must-take 로 함께 차감된다."""
    demand, trading = net_demand_frames(70000, 5000, 4000, wind=1200)

    net = build_net_demand(demand, trading, "subtract_solar_renewable")

    assert net.iloc[0] == pytest.approx(59800.0)


def test_net_demand_requires_wind_column():
    """wind 컬럼이 없으면 조용히 빼먹지 않고 예외를 낸다."""
    demand, trading = net_demand_frames(70000, 5000, 4000)

    with pytest.raises(DataError, match="wind"):
        build_net_demand(demand, trading.drop(columns=["wind"]),
                         "subtract_solar_renewable")


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


# ── 연속 구간 선택 ───────────────────────────────────────────────────────────


def _hourly_series(*hours: int) -> pd.Series:
    """주어진 시(hour)들만 있는 시계열. 빠진 시는 격자 구멍이 된다."""
    index = pd.DatetimeIndex(
        [pd.Timestamp(f"2026-09-20 {h:02d}:00:00", tz="Asia/Seoul") for h in hours],
        name="ts",
    )
    return pd.Series([float(h) for h in hours], index=index, name="net_demand_mw")


def test_contiguous_runs_splits_on_gaps():
    runs = dl.contiguous_runs(_hourly_series(0, 1, 2, 5, 6).index)

    assert runs == [(0, 3), (3, 5)]


def test_latest_contiguous_window_skips_trailing_gap():
    """끝에 구멍이 있으면 최신 데이터를 건너뛰고 연속 구간을 쓴다.

    tail(3) 은 [2, 5, 6] 을 집어 3시간이 아니라 5시간에 걸친 입력을 만든다.
    """
    series = _hourly_series(0, 1, 2, 5, 6)

    window = dl.latest_contiguous_window(series, 3)

    assert window.index.tolist() == series.index[:3].tolist()
    assert window.tolist() == [0.0, 1.0, 2.0]


def test_latest_contiguous_window_prefers_most_recent_run():
    series = _hourly_series(0, 1, 2, 10, 11, 12)

    window = dl.latest_contiguous_window(series, 3)

    assert window.tolist() == [10.0, 11.0, 12.0]


def test_latest_contiguous_window_takes_tail_of_long_run():
    series = _hourly_series(0, 1, 2, 3, 4)

    window = dl.latest_contiguous_window(series, 2)

    assert window.tolist() == [3.0, 4.0]


def test_latest_contiguous_window_reports_longest_run_when_too_short():
    """연속 구간이 모자라면 가장 긴 구간 길이를 메시지에 담는다 (원인 파악용)."""
    series = _hourly_series(0, 1, 2, 5, 6)

    with pytest.raises(DataError, match="가장 긴 연속 구간은 3시간"):
        dl.latest_contiguous_window(series, 4)


def test_wide_trading_pivots_long_format():
    rows = [
        {"ts": "2026-09-20 00:00:00", "fuel": "solar", "trade_mwh": 100.0},
        {"ts": "2026-09-20 00:00:00", "fuel": "nuclear", "trade_mwh": 20000.0},
    ]

    wide = wide_trading(rows)

    assert len(wide) == 1
    assert wide[0]["solar"] == 100.0
    assert wide[0]["nuclear"] == 20000.0


def test_wide_trading_requires_expected_columns():
    with pytest.raises(DataError, match="fuel"):
        wide_trading([{"ts": "2026-09-20 00:00:00", "trade_mwh": 1.0}])


def test_wide_trading_can_pivot_capacity():
    """같은 데이터셋의 capacity_mw 도 같은 함수로 가로형이 된다 (예비율·설비용량용)."""
    rows = [
        {"ts": "2026-09-20 00:00:00", "fuel": "lng", "capacity_mw": 46202.0},
        {"ts": "2026-09-20 00:00:00", "fuel": "nuclear", "capacity_mw": 26050.0},
    ]

    wide = wide_trading(rows, value_column="capacity_mw")

    assert wide[0]["lng"] == 46202.0
    assert wide[0]["nuclear"] == 26050.0


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
        min_sample_ratio=1.0,
        net_demand_mode="subtract_solar_renewable", fuel_cost_month="latest",
    )
    base.update(overrides)
    return Settings(**base)


def _synthetic_series(start: str, hours: int, demand, solar, renewable, wind=None):
    """원시 행을 만든다. 두 데이터셋의 수집 간격이 다른 점까지 재현한다.

    power_demand    5분 간격 (시간당 12행)
    trading_by_fuel 1시간 간격 (시간당 발전원별 1행)
    """
    wind = wind if wind is not None else [0.0] * hours
    demand_rows_, trading_rows_ = [], []
    ts0 = pd.Timestamp(start)
    for h in range(hours):
        for i in range(12):  # 5분 x 12 = 1시간, 완전한 시간으로 맞춤
            ts = (ts0 + pd.Timedelta(hours=h, minutes=i * 5)).strftime("%Y-%m-%d %H:%M:%S")
            demand_rows_.append({"ts": ts, "demand_mw": demand[h]})

        hour_ts = (ts0 + pd.Timedelta(hours=h)).strftime("%Y-%m-%d %H:%M:%S")
        for fuel, series in (("solar", solar), ("wind", wind), ("renewable", renewable)):
            trading_rows_.append({"ts": hour_ts, "fuel": fuel, "trade_mwh": series[h]})
    return demand_rows_, trading_rows_


def test_build_dispatch_input_end_to_end():
    """로더 전체 경로: 원시 행 -> DispatchInput. MILP 가 받는 최종 모양을 검증한다."""
    demand_rows_, trading_rows_ = _synthetic_series(
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
        generators, settings, demand_rows_, trading_rows_, dam_rows_,
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
    demand_rows_, trading_rows_ = _synthetic_series(
        "2026-09-20 00:00:00", hours=5,
        demand=[1.0, 2.0, 3.0, 4.0, 5.0],
        solar=[0.0] * 5, renewable=[0.0] * 5,
    )
    settings = _synthetic_settings(horizon_h=2)

    result = dl.build_dispatch_input(
        [_synthetic_generator()], settings, demand_rows_, trading_rows_,
    )

    assert result.horizon_h == 2
    assert result.net_demand_mw == [4.0, 5.0]


def test_build_dispatch_input_insufficient_history_rejected():
    """원본이 horizon 보다 짧으면 (조용히 패딩하지 않고) 예외를 낸다."""
    demand_rows_, trading_rows_ = _synthetic_series(
        "2026-09-20 00:00:00", hours=2,
        demand=[1.0, 2.0], solar=[0.0, 0.0], renewable=[0.0, 0.0],
    )
    settings = _synthetic_settings(horizon_h=24)

    with pytest.raises(DataError, match="horizon"):
        dl.build_dispatch_input([_synthetic_generator()], settings, demand_rows_, trading_rows_)


def test_build_dispatch_input_timestamps_are_contiguous_despite_gap(caplog):
    """원본에 구멍이 있어도 반환되는 timestamp 는 1시간 등간격이어야 한다.

    MILP 의 램프율(MW/h)·min up/down 은 "인접한 두 점은 1시간 차이"를 전제한다.
    개수만 세서 자르면 구멍을 건너뛴 채 N개를 집어, 제약이 예외 없이 틀린다.
    """
    demand_rows_, trading_rows_ = _synthetic_series(
        "2026-09-20 00:00:00", hours=5,
        demand=[70000.0, 71000.0, 72000.0, 73000.0, 74000.0],
        solar=[0.0] * 5, renewable=[0.0] * 5,
    )
    # 03시의 5분 샘플을 전부 지워 격자에 구멍을 만든다 (연속 누락 재현).
    demand_rows_ = [r for r in demand_rows_ if " 03:" not in r["ts"]]
    settings = _synthetic_settings(horizon_h=3)

    with caplog.at_level("INFO"):
        result = dl.build_dispatch_input(
            [_synthetic_generator()], settings, demand_rows_, trading_rows_,
        )

    ts = result.timestamps
    assert len(ts) == 3
    deltas = {(ts[i] - ts[i - 1]).total_seconds() for i in range(1, len(ts))}
    assert deltas == {3600.0}
    # 04시가 최신이지만 03시 구멍 때문에 00~02시 구간이 선택된다.
    assert ts[-1].hour == 2
    assert "격자 구멍" in caplog.text


def test_build_dispatch_input_disjoint_periods_rejected():
    """수요와 거래량의 수집 구간이 전혀 겹치지 않으면 예외를 낸다.

    두 데이터셋은 수집 구간이 다르다. 겹치는 부분만 쓰는데, 아예 안 겹치면
    빈 결과가 되므로 구간을 메시지에 담아 바로 알 수 있게 한다.
    """
    demand_rows_, _ = _synthetic_series(
        "2026-09-20 00:00:00", hours=2,
        demand=[70000.0, 70000.0], solar=[0.0, 0.0], renewable=[0.0, 0.0],
    )
    _, trading_rows_ = _synthetic_series(
        "2026-10-20 00:00:00", hours=2,   # 한 달 뒤 — 겹치는 시각 없음
        demand=[0.0, 0.0], solar=[0.0, 0.0], renewable=[0.0, 0.0],
    )
    settings = _synthetic_settings(horizon_h=1)

    with pytest.raises(DataError, match="겹치는 시각이 없다"):
        dl.build_dispatch_input(
            [_synthetic_generator()], settings, demand_rows_, trading_rows_,
        )


def test_build_dispatch_input_uses_overlap_of_differing_periods(caplog):
    """구간이 일부만 겹치면 겹치는 쪽만 쓰고, 얼마나 잘렸는지 로그로 남긴다."""
    demand_rows_, _ = _synthetic_series(
        "2026-09-20 00:00:00", hours=5,
        demand=[70000.0] * 5, solar=[0.0] * 5, renewable=[0.0] * 5,
    )
    _, trading_rows_ = _synthetic_series(
        "2026-09-20 03:00:00", hours=2,   # 03:00, 04:00 만 겹친다
        demand=[0.0, 0.0], solar=[1000.0, 2000.0], renewable=[0.0, 0.0],
    )
    settings = _synthetic_settings(horizon_h=2)

    with caplog.at_level("INFO"):
        result = dl.build_dispatch_input(
            [_synthetic_generator()], settings, demand_rows_, trading_rows_,
        )

    assert result.horizon_h == 2
    assert result.net_demand_mw == pytest.approx([69000.0, 68000.0])
    assert "겹치는 구간만 사용" in caplog.text


def test_build_dispatch_input_without_dam_rows_has_no_storage_rate():
    demand_rows_, trading_rows_ = _synthetic_series(
        "2026-09-20 00:00:00", hours=1,
        demand=[70000.0], solar=[0.0], renewable=[0.0],
    )
    settings = _synthetic_settings(horizon_h=1)

    result = dl.build_dispatch_input(
        [_synthetic_generator()], settings, demand_rows_, trading_rows_, dam_rows=None,
    )

    assert result.storage_rate_pct is None


def test_build_dispatch_input_real_csv_data(csv_dir):
    """레포에 커밋된 실제 CSV 로 전체 경로가 깨지지 않는지 (회귀 방지)."""
    settings = _synthetic_settings(horizon_h=24)
    generators = [_synthetic_generator(fuel="lng")]

    result = dl.build_dispatch_input(
        generators, settings,
        dl.read_csv_dataset(csv_dir, "power_demand"),
        dl.read_csv_dataset(csv_dir, "trading_by_fuel"),
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
