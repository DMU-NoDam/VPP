"""시계열 데이터 -> DispatchInput.

소스는 두 가지다. 반환 타입은 같다.
  - CSV 직접 읽기 (기본)  : data/csv/*.csv — 볼륨 공유, 네트워크 불필요
  - collector data_api    : GET http://collector:8000/data/{name} — 옵션

CLAUDE.md 는 볼륨 공유와 REST 를 모두 허용한다. 테스트가 네트워크 없이 돌아가도록
CSV 를 기본으로 두고, 외부 API 장애 시에도 최소 실행이 가능하게 한다.

시간 처리
  - 원본 시각은 naive 문자열("2026-09-22 15:55:00") 이다. Asia/Seoul 로 localize 한다
  - power_demand / generation_by_fuel 은 5분 간격이라 1시간으로 집계한다
  - 불완전한 시간(샘플이 모자란 시간)은 버린다. 평균이 왜곡되기 때문이다
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
    generation_hourly: pd.DataFrame,
    mode: str = "subtract_solar_renewable",
) -> pd.Series:
    """순수요 = 수요 - 비급전 발전량.

    `mode` 가 설정으로 빠져 있는 이유:
    power_demand(= getPwrAmountByGen 의 fuelPwrTot)가 fuelPwr9(태양광)를 포함하지
    않는 것으로 측정됐다. 태양광을 제외하고 합산하면 잔차가 평균 7,240 MW 상수로
    떨어지고 태양광과의 상관이 사라진다(-0.069). 포함 관계가 확정되기 전까지
    차감 방식을 바꿔 끼울 수 있게 둔다. [미정 — 팀 확인 대기]

    pumped(양수)는 차감하지 않는다. 비급전이 아니라 ESS 성격이라 별도 모델링한다.
    """
    demand = demand_hourly["demand_mw"]

    if mode == "demand_only":
        return demand.rename("net_demand_mw")

    if mode == "subtract_renewable_only":
        subtract_fuels = ["renewable"]
    else:
        subtract_fuels = list(constants.MUST_TAKE_FUELS)

    available = [f for f in subtract_fuels if f in generation_hourly.columns]
    missing = sorted(set(subtract_fuels) - set(available))
    if missing:
        raise DataError(
            f"순수요 계산에 필요한 발전원이 없다: {missing} "
            f"(있는 컬럼: {list(generation_hourly.columns)})"
        )

    aligned = generation_hourly[available].reindex(demand.index)
    if aligned.isna().any().any():
        gaps = aligned[aligned.isna().any(axis=1)].index[:3]
        raise DataError(
            f"수요와 발전량의 시각이 맞지 않는다 (예: {[str(g) for g in gaps]})"
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


def wide_generation(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """generation_by_fuel 의 세로형(ts, fuel, mw)을 가로형(ts, <fuel>...)으로."""
    df = pd.DataFrame(rows)
    for column in ("ts", "fuel", "mw"):
        if column not in df.columns:
            raise DataError(f"generation_by_fuel 에 '{column}' 컬럼이 없다")

    wide = df.pivot_table(index="ts", columns="fuel", values="mw", aggfunc="last")
    return wide.reset_index().to_dict("records")


def build_dispatch_input(
    generators: list[Generator],
    settings: Settings,
    demand_rows: list[dict[str, Any]],
    generation_rows: list[dict[str, Any]],
    dam_rows: list[dict[str, Any]] | None = None,
    horizon_h: int | None = None,
) -> DispatchInput:
    """로딩된 행들을 MILP 입력으로 묶는다. horizon 만큼 끝에서 잘라 쓴다."""
    horizon = horizon_h or settings.horizon_h

    demand_hourly = to_hourly(
        demand_rows, "ts", ["demand_mw"],
        timezone=settings.timezone, resample=settings.resample,
    )

    wide_rows = wide_generation(generation_rows)
    fuel_columns = [c for c in constants.FUEL_CODES
                    if any(c in row for row in wide_rows[:1])]
    generation_hourly = to_hourly(
        wide_rows, "ts", fuel_columns,
        timezone=settings.timezone, resample=settings.resample,
    )

    net = build_net_demand(demand_hourly, generation_hourly, settings.net_demand_mode)

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
