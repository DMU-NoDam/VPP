"""시계열 데이터 -> DispatchInput.

소스는 두 가지다. 반환 타입은 같다.
  - CSV 직접 읽기 (기본)  : data/csv/*.csv — 볼륨 공유, 네트워크 불필요
  - collector data_api    : GET http://collector:8000/data/{name} — 옵션

CLAUDE.md 는 볼륨 공유와 REST 를 모두 허용한다. 테스트가 네트워크 없이 돌아가도록
CSV 를 기본으로 두고, 외부 API 장애 시에도 최소 실행이 가능하게 한다.

시간 처리
  - 원본 시각은 naive 문자열("2026-09-22 15:55:00") 이다. Asia/Seoul 로 localize 한다
  - power_demand 는 5분 간격, trading_by_fuel 은 1시간 간격이라 기대 샘플 수가 다르다
    (`to_hourly(source_interval_min=...)`). 섞으면 한쪽이 전부 불완전으로 걸러진다
  - 불완전한 시간(샘플이 모자란 시간)은 버린다. 평균이 왜곡되기 때문이다
  - 두 데이터셋의 수집 구간이 다르므로 겹치는 구간만 쓴다 (`_common_index`)

단위
  - trading_by_fuel 의 값은 거래량(MWh)이다. time step 이 1시간이므로 수치상 MW 와
    같아 그대로 쓰지만, time step 을 바꾸면 이 가정이 깨진다 (CLAUDE.md 단위 규칙)
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import pandas as pd
import requests

import constants
from models import DispatchInput, Generator, Settings

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SEC = 10

# 원본 수집 간격. collector domain.py 의 Dataset.interval_sec 와 일치해야 한다
# (POWER_DEMAND 300s, TRADING_BY_FUEL 3600s). 1시간당 기대 샘플 수를 여기서 정하므로
# 틀리면 그 데이터셋의 모든 시간이 "불완전"으로 걸러져 조용히 빈 결과가 된다.
DEMAND_INTERVAL_MIN = 5
TRADING_INTERVAL_MIN = 60


class DataError(ValueError):
    """시계열 데이터가 모자라거나 모양이 맞지 않을 때."""


# ── 소스: CSV ────────────────────────────────────────────────────────────────


def read_csv_dataset(csv_dir: str | Path, name: str) -> list[dict[str, Any]]:
    """data/csv/{name}.csv 를 행 목록으로. data_api 응답의 `rows` 와 같은 모양이다."""
    path = Path(csv_dir) / f"{name}.csv"
    if not path.exists():
        raise DataError(f"데이터 파일이 없다: {path}")

    df = pd.read_csv(path)
    return df.to_dict("records")


# ── 소스: REST ───────────────────────────────────────────────────────────────


def fetch_dataset(
    base_url: str,
    name: str,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """collector data_api 에서 데이터셋 하나를 받아온다.

    계약은 docs/api_spec.md 의 `GET /data/{name}?start=&end=&limit=` 를 따른다.
    """
    params = {k: v for k, v in (("start", start), ("end", end), ("limit", limit))
              if v is not None}
    url = f"{base_url.rstrip('/')}/data/{name}"

    response = requests.get(url, params=params, timeout=HTTP_TIMEOUT_SEC)
    response.raise_for_status()
    payload = response.json()

    rows = payload.get("rows")
    if rows is None:
        raise DataError(f"{url}: 응답에 rows 가 없다")
    return rows


# ── 시간 정규화 ──────────────────────────────────────────────────────────────


def to_hourly(
    rows: list[dict[str, Any]],
    time_column: str,
    value_columns: list[str],
    timezone: str = constants.DEFAULT_TIMEZONE,
    resample: str = "mean",
    source_interval_min: int = 5,
    min_sample_ratio: float = 1.0,
) -> pd.DataFrame:
    """행 목록을 1시간 간격 DataFrame 으로. 인덱스는 tz-aware 정시다.

    `resample`
      mean    : 시간평균. 1시간 에너지 균형과 의미가 맞아 기본값이다 [팀 확정 2026-10-01]
      instant : 정시값(그 시간의 첫 샘플)

    `min_sample_ratio` 는 그 시간을 받아들일 최소 샘플 비율이다. 5분 간격이면
    1시간에 12개가 와야 하는데 원본에 빠진 시각이 있다. 모자란 시간을 그대로
    평균내면 그 시간만 다른 모집단의 평균이 된다. 0 이면 필터를 끈다.

    기준을 1.0 에서 낮춘 근거 [2026-10-11]: KPX sukub.do 원본을 직접 조회해 보면
    빠진 시각은 "값이 빈 칸"이 아니라 "행 자체가 없음"이다 (조회 3일 모두 빈 칸
    0개). 단발 누락(주로 23:55, 전체 결측의 26%)이면 11/12 샘플의 평균은 12/12 와
    사실상 같으므로 버릴 이유가 없다. 반면 수십 분짜리 연속 누락(예 2026-06-29
    23:20~23:55)은 비율로 걸러지며, 그래도 남는 격자 구멍은
    `latest_contiguous_window` 가 처리한다.
    """
    if not rows:
        raise DataError(f"빈 데이터 (time_column={time_column})")

    df = pd.DataFrame(rows)
    for column in [time_column, *value_columns]:
        if column not in df.columns:
            raise DataError(f"컬럼 '{column}' 이 없다 (있는 컬럼: {list(df.columns)})")

    ts = pd.to_datetime(df[time_column], format=constants.TS_FORMAT, errors="coerce")
    if ts.isna().any():
        bad = df.loc[ts.isna(), time_column].head(3).tolist()
        raise DataError(f"{time_column} 파싱 실패 (형식 {constants.TS_FORMAT}): 예 {bad}")

    df = df.assign(**{time_column: ts.dt.tz_localize(timezone)})
    df = df.set_index(time_column).sort_index()

    numeric = df[value_columns].apply(pd.to_numeric, errors="coerce")
    grouped = numeric.resample("1h")

    hourly = grouped.mean() if resample == "mean" else grouped.first()

    if min_sample_ratio > 0:
        expected = constants.MINUTES_PER_HOUR // source_interval_min
        threshold = max(1, math.ceil(expected * min_sample_ratio))
        counts = grouped.count().min(axis=1)
        incomplete = counts[counts < threshold]
        if len(incomplete):
            logger.info(
                "불완전한 시간 %d개 제외 (기대 %d개 중 %d개 이상 필요): %s%s",
                len(incomplete), expected, threshold,
                [str(i) for i in incomplete.index[:3]],
                " ..." if len(incomplete) > 3 else "",
            )
        hourly = hourly[counts >= threshold]

    hourly = hourly.dropna()
    if hourly.empty:
        raise DataError("1시간 집계 결과가 비었다 — 원본 간격이나 결측을 확인할 것")
    return hourly


# ── 순수요 ───────────────────────────────────────────────────────────────────


def build_net_demand(
    demand_hourly: pd.DataFrame,
    trading_hourly: pd.DataFrame,
    mode: str = "subtract_solar_renewable",
) -> pd.Series:
    """순수요 = 수요 - 비급전 거래량.

    power_demand 가 태양광을 포함하는지가 한동안 [미정] 이었다. 1년치(4,416h)로
    재측정해 포함하는 것으로 확정했다 [2026-10-09]:
      - solar 포함 합산 -> 잔차와 solar 의 상관 -0.193 (무상관)
      - solar 제외 합산 -> 잔차와 solar 의 상관 +0.830 (강한 상관)
    즉 수요에서 태양광을 빼지 않으면 태양광만큼이 잔차로 남는다. 3일 샘플로는
    반대 결론(-0.069)이 나왔는데 표본이 모자랐던 것이다.
    남는 잔차 평균 2,818 MW 는 corr(pumped)=0.45 로 양수 펌핑 부하 쪽이다.

    solar / wind / renewable 은 서로 겹치지 않는다 (corr(renewable, wind) -0.049,
    renewable 은 1,836~2,680 MW 로 거의 상수인 기저 신재생). 셋 다 차감한다.

    pumped(양수)는 차감하지 않는다. 비급전이 아니라 ESS 성격이라 별도 모델링한다.

    `mode` 는 포함 관계가 다시 바뀔 때 갈아끼울 수 있도록 남겨둔다.
    """
    demand = demand_hourly["demand_mw"]

    if mode == "demand_only":
        return demand.rename("net_demand_mw")

    if mode == "subtract_renewable_only":
        subtract_fuels = ["renewable"]
    else:
        subtract_fuels = list(constants.MUST_TAKE_FUELS)

    available = [f for f in subtract_fuels if f in trading_hourly.columns]
    missing = sorted(set(subtract_fuels) - set(available))
    if missing:
        raise DataError(
            f"순수요 계산에 필요한 발전원이 없다: {missing} "
            f"(있는 컬럼: {list(trading_hourly.columns)})"
        )

    aligned = trading_hourly[available].reindex(demand.index)
    if aligned.isna().any().any():
        gaps = aligned[aligned.isna().any(axis=1)].index[:3]
        raise DataError(
            f"수요와 거래량의 시각이 맞지 않는다 (예: {[str(g) for g in gaps]}). "
            "구간이 다른 두 데이터셋이면 _common_index 로 먼저 잘라야 한다"
        )

    net = demand - aligned.sum(axis=1)
    if (net < 0).any():
        worst = net.min()
        raise DataError(
            f"순수요가 음수가 되는 시각이 있다 (최소 {worst:.1f} MW). "
            f"net_demand_mode='{mode}' 가 이중 차감일 수 있다"
        )
    return net.rename("net_demand_mw")


# ── 조립 ─────────────────────────────────────────────────────────────────────


TRADING_VALUE_COLUMN = "trade_mwh"


def wide_trading(
    rows: list[dict[str, Any]], value_column: str = TRADING_VALUE_COLUMN
) -> list[dict[str, Any]]:
    """trading_by_fuel 의 세로형(ts, fuel, <value>)을 가로형(ts, <fuel>...)으로.

    `value_column` 기본값은 거래량(`trade_mwh`)이다. 같은 데이터셋에 발전원별
    공급능력(`capacity_mw`)도 있어서, 예비율·설비용량 쪽에서 같은 함수를 재사용한다.
    """
    df = pd.DataFrame(rows)
    for column in ("ts", "fuel", value_column):
        if column not in df.columns:
            raise DataError(
                f"trading_by_fuel 에 '{column}' 컬럼이 없다 (있는 컬럼: {list(df.columns)})"
            )

    wide = df.pivot_table(index="ts", columns="fuel", values=value_column, aggfunc="last")
    return wide.reset_index().to_dict("records")


HOUR_STEP = pd.Timedelta(hours=1)


def contiguous_runs(index: pd.DatetimeIndex) -> list[tuple[int, int]]:
    """1시간 등간격으로 이어지는 구간들의 [시작, 끝) 위치 목록. 시간 오름차순."""
    if len(index) == 0:
        return []

    starts = [0]
    starts += [i for i in range(1, len(index)) if index[i] - index[i - 1] != HOUR_STEP]
    starts.append(len(index))
    return [(starts[k], starts[k + 1]) for k in range(len(starts) - 1)]


def latest_contiguous_window(series: pd.Series, horizon_h: int) -> pd.Series:
    """1시간 등간격이 horizon_h 개 이어지는 가장 최근 구간.

    `tail(horizon_h)` 을 쓰면 안 되는 이유: 원본 결측 때문에 1시간 격자에 구멍이
    생기는데, 개수만 세면 그 구멍을 건너뛴 채 24개를 집는다. 실제로 그렇게 뽑힌
    24개가 26시간에 걸쳐 있고 2시간 점프가 2곳 있었다 [2026-10-11].

    램프율(MW/h)과 min up/down 제약은 "인접한 두 점은 1시간 차이"를 전제한다.
    구멍이 섞이면 solver 는 아무 불만 없이 풀고 제약만 조용히 틀린다. 그래서
    개수가 아니라 연속성으로 구간을 고른다.

    KPI 측정 스크립트도 여러 구간을 비교할 때 이 함수를 재사용한다.
    """
    if horizon_h < 1:
        raise DataError(f"horizon 은 1 이상이어야 한다 (받은 값 {horizon_h})")

    runs = contiguous_runs(series.index)
    for begin, stop in reversed(runs):
        if stop - begin >= horizon_h:
            window = series.iloc[stop - horizon_h:stop]
            if stop != len(series):
                logger.info(
                    "격자 구멍 때문에 최신 %d시간을 건너뛰고 %s ~ %s 를 썼다",
                    len(series) - stop, window.index[0], window.index[-1],
                )
            return window

    longest = max((stop - begin for begin, stop in runs), default=0)
    raise DataError(
        f"연속 {horizon_h}시간 구간이 없다. 가장 긴 연속 구간은 {longest}시간이고 "
        f"전체 {len(series)}시간이 {len(runs)}개 조각으로 끊겨 있다. "
        "원본 결측 때문이다 — horizon 을 줄이거나 데이터를 보강할 것"
    )


def _common_index(*frames: pd.DataFrame) -> pd.DatetimeIndex:
    """여러 데이터셋에 공통으로 있는 시각. 수집 구간이 다를 때 쓴다.

    power_demand 와 trading_by_fuel 은 수집 구간이 다르다 (2026-10-09 기준 수요는
    2026-10 까지, 거래량은 2025-12 까지). 한쪽 기준으로 reindex 하면 반대쪽이 전부
    결측이 되므로, 겹치는 구간만 남긴다. 얼마나 잘렸는지는 로그로 남긴다 —
    조용히 10개월을 버리면 KPI 측정 기간을 착각한다.
    """
    index = frames[0].index
    for frame in frames[1:]:
        index = index.intersection(frame.index)
    if index.empty:
        raise DataError(
            "데이터셋 간 겹치는 시각이 없다 "
            + " / ".join(f"{f.index.min()}~{f.index.max()} ({len(f)}h)" for f in frames)
        )

    dropped = [len(f) - len(index) for f in frames]
    if any(dropped):
        logger.info(
            "겹치는 구간만 사용: %s ~ %s (%d시간). 데이터셋별 제외 %s",
            index.min(), index.max(), len(index), dropped,
        )
    return index


def build_dispatch_input(
    generators: list[Generator],
    settings: Settings,
    demand_rows: list[dict[str, Any]],
    trading_rows: list[dict[str, Any]],
    dam_rows: list[dict[str, Any]] | None = None,
    horizon_h: int | None = None,
) -> DispatchInput:
    """로딩된 행들을 MILP 입력으로 묶는다.

    horizon 은 "끝에서 N개"가 아니라 "연속 N시간이 확보되는 가장 최근 구간"이다
    (`latest_contiguous_window`). 반환되는 timestamps 는 반드시 1시간 등간격이다 —
    MILP 의 램프·최소시간 제약이 그걸 전제한다.
    """
    horizon = horizon_h or settings.horizon_h

    demand_hourly = to_hourly(
        demand_rows, "ts", ["demand_mw"],
        timezone=settings.timezone, resample=settings.resample,
        source_interval_min=DEMAND_INTERVAL_MIN,
        min_sample_ratio=settings.min_sample_ratio,
    )

    wide_rows = wide_trading(trading_rows)
    fuel_columns = [c for c in constants.FUEL_CODES
                    if any(c in row for row in wide_rows[:1])]
    trading_hourly = to_hourly(
        wide_rows, "ts", fuel_columns,
        timezone=settings.timezone, resample=settings.resample,
        source_interval_min=TRADING_INTERVAL_MIN,
        min_sample_ratio=settings.min_sample_ratio,
    )

    # 두 데이터셋의 수집 구간이 다르다. 겹치는 구간으로 먼저 자른다.
    index = _common_index(demand_hourly, trading_hourly)
    net = build_net_demand(
        demand_hourly.loc[index], trading_hourly.loc[index], settings.net_demand_mode
    )

    if len(net) < horizon:
        raise DataError(
            f"1시간 집계 결과가 {len(net)}시간뿐이라 horizon {horizon}시간을 못 채운다"
        )
    net = latest_contiguous_window(net, horizon)

    # 구성상 등간격이 보장되지만, MILP 가 틀린 입력을 받는 경로를 한 번 더 막는다.
    gaps = [
        (net.index[i - 1], net.index[i])
        for i in range(1, len(net))
        if net.index[i] - net.index[i - 1] != HOUR_STEP
    ]
    if gaps:
        raise DataError(
            f"timestamp 가 1시간 등간격이 아니다 ({len(gaps)}곳): "
            f"{[f'{a} -> {b}' for a, b in gaps[:3]]}. "
            "램프·최소시간 제약이 조용히 틀리므로 MILP 에 넘기지 않는다"
        )

    return DispatchInput(
        generators=generators,
        timestamps=[ts.to_pydatetime() for ts in net.index],
        net_demand_mw=[float(v) for v in net.to_numpy()],
        reserve_margin_ratio=settings.reserve_margin_ratio,
        storage_rate_pct=_latest_storage_rate(dam_rows),
    )


def _latest_storage_rate(dam_rows: list[dict[str, Any]] | None) -> float | None:
    """가장 최근 저수율(%). 수력 출력 제한 판정에 쓴다.

    댐이 여러 개면 평균을 쓴다. [미정] 대표 댐 선정과 가중 방식은 팀 결정 필요.
    현재 수집된 댐은 소양강(1012110) 하나뿐이라 평균이 곧 그 값이다.
    """
    if not dam_rows:
        return None

    df = pd.DataFrame(dam_rows)
    if "storage_rate_pct" not in df.columns or "observed_at" not in df.columns:
        raise DataError("dam_status 에 observed_at / storage_rate_pct 컬럼이 필요하다")

    latest = df[df.observed_at == df.observed_at.max()]
    rate = pd.to_numeric(latest.storage_rate_pct, errors="coerce").mean()
    return None if pd.isna(rate) else float(rate)
