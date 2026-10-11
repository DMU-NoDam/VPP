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
    require_complete: bool = True,
) -> pd.DataFrame:
    """행 목록을 1시간 간격 DataFrame 으로. 인덱스는 tz-aware 정시다.

    `resample`
      mean    : 시간평균. 1시간 에너지 균형과 의미가 맞아 기본값이다 [팀 확정 2026-10-01]
      instant : 정시값(그 시간의 첫 샘플)

    `require_complete` 가 True 면 샘플이 모자란 시간을 버린다. 5분 간격이면
    1시간에 12개가 와야 하는데, 원본에 결측 구간이 있어 모자란 시간이 생긴다.
    그대로 평균을 내면 그 시간만 다른 모집단의 평균이 된다.
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

    if require_complete:
        expected = constants.MINUTES_PER_HOUR // source_interval_min
        counts = grouped.count().min(axis=1)
        incomplete = counts[counts < expected]
        if len(incomplete):
            logger.info(
                "불완전한 시간 %d개 제외 (기대 샘플 %d개): %s%s",
                len(incomplete), expected,
                [str(i) for i in incomplete.index[:3]],
                " ..." if len(incomplete) > 3 else "",
            )
        hourly = hourly[counts >= expected]

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
    """로딩된 행들을 MILP 입력으로 묶는다. horizon 만큼 끝에서 잘라 쓴다."""
    horizon = horizon_h or settings.horizon_h

    demand_hourly = to_hourly(
        demand_rows, "ts", ["demand_mw"],
        timezone=settings.timezone, resample=settings.resample,
        source_interval_min=DEMAND_INTERVAL_MIN,
    )

    wide_rows = wide_trading(trading_rows)
    fuel_columns = [c for c in constants.FUEL_CODES
                    if any(c in row for row in wide_rows[:1])]
    trading_hourly = to_hourly(
        wide_rows, "ts", fuel_columns,
        timezone=settings.timezone, resample=settings.resample,
        source_interval_min=TRADING_INTERVAL_MIN,
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
    net = net.tail(horizon)

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
