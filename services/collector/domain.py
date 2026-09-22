"""데이터 정의.

무엇을 어떤 모양으로 담는지만 적는다. 파일을 읽거나 쓰지 않고, API 도 호출하지 않는다.
store.py 와 collector.py 가 이 정의를 보고 동작한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

# --- 발전원 ------------------------------------------------------------------


@dataclass(frozen=True)
class Fuel:
    """발전원 1종."""

    code: str       # 저장에 쓰는 코드값
    name_ko: str    # 사람이 읽는 이름
    api_field: str  # getPwrAmountByGen 응답 필드명


# getPwrAmountByGen (계통기준) 기준 9종.
# 가스=lng, 국내탄=anthracite 로 둬서 fuel_cost 의 연료명과 맞물리게 했다.
FUELS: tuple[Fuel, ...] = (
    Fuel("hydro",           "수력",   "fuelPwr1"),
    Fuel("oil",             "유류",   "fuelPwr2"),
    Fuel("bituminous_coal", "유연탄", "fuelPwr3"),
    Fuel("nuclear",         "원자력", "fuelPwr4"),
    Fuel("pumped",          "양수",   "fuelPwr5"),  # 펌핑 중에는 음수
    Fuel("lng",             "가스",   "fuelPwr6"),
    Fuel("anthracite",      "국내탄", "fuelPwr7"),
    Fuel("renewable",       "신재생", "fuelPwr8"),
    Fuel("solar",           "태양광", "fuelPwr9"),
)

# API 응답 필드 -> 코드값. 응답 파싱할 때 쓴다.
FUEL_BY_API_FIELD: dict[str, str] = {f.api_field: f.code for f in FUELS}

# 코드값 목록. 값 검증에 쓴다.
FUEL_CODES: tuple[str, ...] = tuple(f.code for f in FUELS)


# --- 데이터셋 ----------------------------------------------------------------

MINUTE = 60
HOUR = 60 * MINUTE


@dataclass(frozen=True)
class Dataset:
    """CSV 파일 하나의 정의."""

    name: str                    # 데이터셋 이름 (파일명, API 경로에 그대로 씀)
    columns: tuple[str, ...]     # CSV 헤더 순서
    key: tuple[str, ...]         # 중복 판정 기준 컬럼
    time_column: str             # 결측 탐지에 쓸 시간 컬럼
    interval_sec: int | None     # 기대 간격. 월 단위는 None

    @property
    def filename(self) -> str:
        return f"{self.name}.csv"


POWER_DEMAND = Dataset(
    name="power_demand",
    columns=("ts", "demand_mw"),
    key=("ts",),
    time_column="ts",
    interval_sec=5 * MINUTE,
)

GENERATION_BY_FUEL = Dataset(
    name="generation_by_fuel",
    columns=("ts", "fuel", "mw"),
    key=("ts", "fuel"),
    time_column="ts",
    interval_sec=5 * MINUTE,
)

SMP = Dataset(
    name="smp",
    columns=("ts", "smp_won_per_kwh"),
    key=("ts",),
    time_column="ts",
    interval_sec=HOUR,
)

FUEL_COST = Dataset(
    name="fuel_cost",
    columns=("month", "fuel", "cost_won_per_kwh"),
    key=("month", "fuel"),
    time_column="month",
    interval_sec=None,  # 월 단위라 초로 표현하지 않는다
)

WEATHER = Dataset(
    name="weather",
    columns=(
        "observed_at",
        "station_id",
        "wind_speed_ms",
        "temperature_c",
        "humidity_pct",
        "solar_radiation_mj",
    ),
    key=("observed_at", "station_id"),
    time_column="observed_at",
    interval_sec=HOUR,
)

DAM_STATUS = Dataset(
    name="dam_status",
    columns=(
        "observed_at",
        "dam_code",
        "water_level_m",
        "inflow_cms",
        "total_discharge_cms",
        "storage_mcm",
        "storage_rate_pct",
    ),
    key=("observed_at", "dam_code"),
    time_column="observed_at",
    interval_sec=10 * MINUTE,
)

# store.load() 가 이 순서대로 읽는다.
DATASETS: tuple[Dataset, ...] = (
    POWER_DEMAND,
    GENERATION_BY_FUEL,
    SMP,
    FUEL_COST,
    WEATHER,
    DAM_STATUS,
)

DATASET_BY_NAME: dict[str, Dataset] = {d.name: d for d in DATASETS}


# --- 결측 구간 ---------------------------------------------------------------


@dataclass(frozen=True)
class Gap:
    """채워야 할 구간 하나. 데이터셋 단위로 나온다."""

    dataset: str
    start: datetime
    end: datetime


# --- 메모리 저장소 -----------------------------------------------------------


@dataclass
class Tables:
    """메모리에 올린 데이터셋 모음.

    파일은 모른다. 디스크에서 읽고 쓰는 일은 store.py 가 맡는다.
    main 이 add 로 채우고, data_api 는 get 으로 읽는다.
    """

    frames: dict[str, pd.DataFrame]  # 데이터셋 이름 -> 표

    def get(self, name: str, start: str | None = None, end: str | None = None):
        """데이터셋 하나를 돌려준다. start/end 를 주면 시간 컬럼으로 걸러서 준다."""
        ds = DATASET_BY_NAME[name]
        df = self.frames[name]
        if start is not None:
            df = df[df[ds.time_column] >= start]
        if end is not None:
            df = df[df[ds.time_column] <= end]
        return df

    def add(self, name: str, rows) -> int:
        """행을 넣는다. key 가 같은 행이 이미 있으면 새 행으로 덮는다.

        반환값은 이번 호출로 늘어난 행 수 (덮어쓴 건 세지 않는다).
        """
        ds = DATASET_BY_NAME[name]
        if not rows:
            return 0

        old = self.frames.get(name)
        before = 0 if old is None else len(old)

        new = pd.DataFrame(rows).reindex(columns=list(ds.columns))
        merged = new if before == 0 else pd.concat([old, new], ignore_index=True)
        merged = merged.drop_duplicates(subset=list(ds.key), keep="last")
        merged = merged.sort_values(list(ds.key), kind="stable").reset_index(drop=True)

        # 읽는 쪽(data_api)이 락 없이 일관된 상태를 보도록 표를 통째로 갈아끼운다.
        self.frames = {**self.frames, name: merged}
        return len(merged) - before

    def find_gaps(self, now: datetime) -> list[Gap]:
        """데이터셋마다 "가지고 있는 마지막 시각 ~ now" 를 결측으로 돌려준다.

        데이터 중간에 뚫린 구멍은 보지 않는다. 원본 CSV 부터 빠져 있는 구간이 많아서,
        매번 다시 받으려 해봐야 API 에도 없는 구간을 계속 두드리게 된다.
        (팀에서 원본 결측 처리 방침이 정해지면 그때 다시 본다)
        """
        gaps: list[Gap] = []
        for ds in DATASETS:
            if ds.interval_sec is None:
                continue  # 월 단위(fuel_cost)는 API 가 없다

            step = timedelta(seconds=ds.interval_sec)
            ts = pd.to_datetime(self.frames[ds.name][ds.time_column], format=TS_FORMAT)
            last = ts.max().to_pydatetime()
            if now - last >= step:
                gaps.append(Gap(ds.name, last + step, now))
        return gaps

    def summary(self) -> str:
        """적재 결과 한 줄 요약. main.py 가 기동 로그에 찍는다."""
        return ", ".join(f"{n}={len(df):,}행" for n, df in self.frames.items())


# --- 포맷 --------------------------------------------------------------------

TS_FORMAT = "%Y-%m-%d %H:%M:%S"  # ts, observed_at
MONTH_FORMAT = "%Y-%m"           # fuel_cost.month

# 기상청 응답의 결측 표기. 읽을 때 None 으로 바꾼다.
# 기온(TA)도 같은 표기를 쓰기 때문에 실제 -9.0 C 관측값은 결측으로 버려진다. 서울(108)
# 기준 드문 값이라 일단 이대로 두고, 지점을 늘릴 때 다시 본다.
KMA_MISSING = ("-9", "-9.0", "-9.00")
