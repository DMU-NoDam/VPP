"""
데이터 접근 계층 (Data Access Layer)

collector가 쌓아둔 실제 CSV(VPP/data/csv/*.csv)를 읽어서 쓴다.

collector의 domain.py 기준 실제 컬럼:
    power_demand.csv    : ts, demand_mw                          (5분 간격)
    trading_by_fuel.csv : ts, fuel, capacity_mw, trade_mwh        (1시간 간격, long format)
    weather.csv         : observed_at, station_id, wind_speed_ms,
                          temperature_c, humidity_pct, solar_radiation_mj (1시간 간격, 13개 지점)
    결측 표기는 문자열 "None" (domain.MISSING)

target별로 반환되는 DataFrame (1시간 간격):
- demand : datetime, demand_mw, temperature_c(전 지점 평균 기온)
- solar  : datetime, solar_mw, capacity_mw, irradiance_108 ... irradiance_143, irradiance(지점 평균)
- wind   : datetime, wind_mw,  capacity_mw, wind_speed_100 ... wind_speed_252, wind_speed(지점 평균)

단위 메모:
- trade_mwh 는 1시간 거래량(MWh)이라 그 시간의 평균 출력(MW)과 수치가 같다 → solar_mw/wind_mw로 사용
- capacity_mw 는 그 시각의 설비용량(MW). 태양광/풍력 설비가 계속 늘어서 이용률 계산과 nMAE에 쓴다
- irradiance 는 기상청 일사량(MJ/m², 시간합). 밤에는 "None"으로 들어와서 0으로 채운다
- wind_speed 는 m/s, temperature_c 는 ℃
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

MISSING = "None"  # collector domain.MISSING 과 동일한 결측 표기


def _find_data_dir() -> Path:
    """로컬(VPP/data/csv)과 Docker(/app/data/csv, docker-compose 볼륨) 둘 다 지원.
    FORECAST_DATA_DIR 환경변수가 있으면 그걸 우선 사용한다."""
    env = os.getenv("FORECAST_DATA_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    candidates = [
        here.parents[1] / "data" / "csv",  # 로컬: VPP/services/forecast_api -> VPP/data/csv
        here / "data" / "csv",             # Docker: /app/data/csv
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


DATA_DIR = _find_data_dir()

# lag_168h(1주일 전 값) feature를 쓰려면 최소 168시간 + 여유분이 필요하다.
# 이보다 적으면 학습에 못 쓰므로 목업으로 대체한다.
MIN_HOURS_REQUIRED = 24 * 10  # 168h + 여유 72h

SOLAR_FUEL_CODE = "solar"
WIND_FUEL_CODE = "wind"

# 기상 관측 지점 (collector sources/kma_weather.py 의 STATION_IDS 와 맞춘다)
# 태양광: 설비가 많은 지역의 일사량 / 풍력: 풍력단지 근처의 풍속
SOLAR_STATIONS = (108, 165, 156, 146, 129, 136, 143)  # 서울, 목포, 광주, 전주, 서산, 안동, 대구
WIND_STATIONS = (100, 216, 277, 185, 188, 252)        # 대관령, 태백, 영덕, 고산, 성산, 영광군

# target -> (발전원 코드, weather 원본 컬럼, 반환 컬럼 접두어, 지점 목록)
# 반환 컬럼: 지점별 "{접두어}_{지점번호}" + 지점 평균 "{접두어}"
#   예) irradiance_165, ..., irradiance(평균)
_RENEWABLE = {
    "solar": (SOLAR_FUEL_CODE, "solar_radiation_mj", "irradiance", SOLAR_STATIONS),
    "wind": (WIND_FUEL_CODE, "wind_speed_ms", "wind_speed", WIND_STATIONS),
}
TEMPERATURE_COLUMN = "temperature_c"


def weather_columns(target: str) -> list[str]:
    """target이 쓰는 기상 컬럼 이름 목록.
    demand: 기온(전 지점 평균) / solar·wind: 지점별 + 지점 평균."""
    if target == "demand":
        return [TEMPERATURE_COLUMN]
    _, _, prefix, stations = _RENEWABLE[target]
    return [f"{prefix}_{s}" for s in stations] + [prefix]


def _hourly_index(start: str, end: str) -> pd.DatetimeIndex:
    return pd.date_range(start, end, freq="h")


# --- 목업 (실데이터 부족/장애 시 폴백용으로 남겨둠) --------------------------------


def make_synthetic_demand(start: str = "2026-01-01", end: str = "2026-03-01") -> pd.DataFrame:
    dates = _hourly_index(start, end)
    hour = dates.hour
    dayofweek = dates.dayofweek
    n = len(dates)
    weekday_boost = np.where(dayofweek < 5, 1.0, 0.85)
    daily_pattern = 10000 * np.sin((hour - 6) / 24 * 2 * np.pi)
    demand = (55000 + daily_pattern) * weekday_boost + np.random.normal(0, 1500, n)
    temperature = 15 + 8 * np.sin((hour - 9) / 24 * 2 * np.pi) + np.random.normal(0, 1, n)
    return pd.DataFrame({"datetime": dates, "demand_mw": demand.round(1),
                         TEMPERATURE_COLUMN: temperature.round(1)})


def make_synthetic_solar(start: str = "2026-01-01", end: str = "2026-03-01") -> pd.DataFrame:
    dates = _hourly_index(start, end)
    hour = dates.hour
    n = len(dates)
    daylight = np.clip(np.sin((hour - 6) / 12 * np.pi), 0, None)
    irradiance = np.clip(daylight * 3.0 + np.random.normal(0, 0.1, n), 0, None)  # MJ/m² 스케일
    solar_mw = np.clip(irradiance * 3000 + np.random.normal(0, 100, n), 0, None)
    return pd.DataFrame({"datetime": dates, "solar_mw": solar_mw.round(1),
                         "irradiance": irradiance.round(2), "capacity_mw": 12000.0})


def make_synthetic_wind(start: str = "2026-01-01", end: str = "2026-03-01") -> pd.DataFrame:
    dates = _hourly_index(start, end)
    n = len(dates)
    wind_speed = np.clip(np.random.gamma(shape=2.0, scale=1.5, size=n), 0, 25)
    wind_mw = np.clip(500 + wind_speed * 250 + np.random.normal(0, 100, n), 0, None)
    return pd.DataFrame({"datetime": dates, "wind_mw": wind_mw.round(1),
                         "wind_speed": wind_speed.round(2), "capacity_mw": 3500.0})


_SYNTHETIC = {"demand": make_synthetic_demand, "solar": make_synthetic_solar,
              "wind": make_synthetic_wind}


# --- 실제 CSV 로딩 -----------------------------------------------------------


@lru_cache(maxsize=4)
def _read_csv_cached(path: str, mtime: float, time_col: str) -> pd.DataFrame:
    """CSV를 읽어서 캐시한다. 파일 수정시각(mtime)을 키에 넣어서,
    collector가 파일을 갱신하면 자동으로 다시 읽는다. (weather.csv는 15만 행이 넘어서 느림)"""
    df = pd.read_csv(path, parse_dates=[time_col], na_values=[MISSING])
    return df.rename(columns={time_col: "datetime"})


def _read(name: str, time_col: str) -> pd.DataFrame:
    path = DATA_DIR / f"{name}.csv"
    return _read_csv_cached(str(path), path.stat().st_mtime, time_col)


def _read_power_demand() -> pd.DataFrame:
    return _read("power_demand", "ts")


def _read_trading_by_fuel(fuel: str) -> pd.DataFrame:
    df = _read("trading_by_fuel", "ts")
    df = df[df["fuel"] == fuel][["datetime", "trade_mwh", "capacity_mw"]]
    return df.rename(columns={"trade_mwh": "mw"})


def _read_weather() -> pd.DataFrame:
    return _read("weather", "observed_at")


def _filter_range(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    return df[(df["datetime"] >= start) & (df["datetime"] <= end)]


def _to_hourly(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """시간 단위 평균으로 맞추고, 중간에 빈 시간은 선형 보간한다."""
    h = df.set_index("datetime")[cols].resample("h").mean()
    h = h.interpolate(limit_direction="both")
    return h.reset_index()


def _fallback(target: str, n_hours: int, start: str, end: str) -> pd.DataFrame:
    print(f"[data_client] 경고: {target} 실데이터가 {n_hours}시간뿐 "
          f"(최소 {MIN_HOURS_REQUIRED}시간 필요) → 목업으로 대체")
    df = _SYNTHETIC[target](start, end)
    if target in _RENEWABLE:
        # 목업은 평균 컬럼 하나뿐이라, 지점별 컬럼은 같은 값으로 채워 컬럼 구성을 맞춘다
        prefix = _RENEWABLE[target][2]
        for col in weather_columns(target)[:-1]:
            df[col] = df[prefix]
    return df


def get_weather(target: str, start: str, end: str) -> pd.DataFrame:
    """target이 쓰는 기상 변수를 시간 단위 DataFrame(index=datetime)으로 반환.

    컬럼은 weather_columns(target) 참고.
    forecast_engine이 "미래 구간의 기상 예보" 자리에도 쓴다. 지금은 예보 API가 없어서
    해당 시각의 관측값을 예보값 대용으로 쓰고, 관측값도 없으면 빈 DataFrame을 돌려준다.
    """
    cols = weather_columns(target)
    w = _filter_range(_read_weather(), start, end)
    empty = pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], name="datetime"), dtype=float)

    if target == "demand":
        if w.empty:
            return empty
        # 전국 13개 지점 평균 기온 (특정 지역 쏠림을 줄이기 위해 평균 사용)
        t = w.groupby("datetime")[TEMPERATURE_COLUMN].mean().resample("h").mean()
        out = t.interpolate(limit_direction="both").to_frame(TEMPERATURE_COLUMN)
        out.index.name = "datetime"
        return out[cols]

    _, src_col, prefix, stations = _RENEWABLE[target]
    w = w[w["station_id"].isin(stations)]
    if w.empty:
        return empty

    wide = w.pivot_table(index="datetime", columns="station_id", values=src_col, aggfunc="mean",
                         dropna=False)
    wide = wide.reindex(columns=list(stations))
    if target == "solar":
        wide = wide.fillna(0.0)  # 밤 일사량 "None" → 0
    wide = wide.resample("h").mean().interpolate(limit_direction="both")
    wide.columns = [f"{prefix}_{s}" for s in stations]
    wide[prefix] = wide.mean(axis=1)  # 지점 평균
    wide.index.name = "datetime"
    return wide[cols]


def _attach_weather(df: pd.DataFrame, target: str, start: str, end: str) -> pd.DataFrame:
    cols = weather_columns(target)
    weather = get_weather(target, start, end).reset_index()
    merged = pd.merge(df, weather, on="datetime", how="left")
    merged[cols] = merged[cols].interpolate(limit_direction="both").fillna(0.0)
    return merged


def get_demand(start: str, end: str) -> pd.DataFrame:
    """수요 이력(5분 → 1시간 평균) + 전국 평균 기온."""
    df = _filter_range(_read_power_demand(), start, end)
    hourly = _to_hourly(df, ["demand_mw"]) if len(df) else df
    if len(hourly) < MIN_HOURS_REQUIRED:
        return _fallback("demand", len(hourly), start, end)
    return _attach_weather(hourly, "demand", start, end)


def _get_renewable(target: str, start: str, end: str) -> pd.DataFrame:
    fuel = _RENEWABLE[target][0]
    target_col = f"{target}_mw"

    gen = _filter_range(_read_trading_by_fuel(fuel), start, end)
    hourly = _to_hourly(gen, ["mw", "capacity_mw"]) if len(gen) else gen
    if len(hourly) < MIN_HOURS_REQUIRED:
        return _fallback(target, len(hourly), start, end)

    hourly = hourly.rename(columns={"mw": target_col})
    hourly[target_col] = hourly[target_col].clip(lower=0)
    merged = _attach_weather(hourly, target, start, end)
    return merged[["datetime", target_col, "capacity_mw", *weather_columns(target)]]


def get_solar(start: str, end: str) -> pd.DataFrame:
    """태양광 발전량·설비용량(trading_by_fuel, fuel=solar) + 7개 지점 일사량."""
    return _get_renewable("solar", start, end)


def get_wind(start: str, end: str) -> pd.DataFrame:
    """풍력 발전량·설비용량(trading_by_fuel, fuel=wind) + 6개 지점 풍속."""
    return _get_renewable("wind", start, end)


def available_range(target: str) -> tuple[str, str]:
    """target별로 실제 CSV에 들어있는 기간(start, end)을 반환. 학습 스크립트 기본 기간으로 쓴다."""
    if target == "demand":
        dt = _read_power_demand()["datetime"]
    else:
        dt = _read_trading_by_fuel(_RENEWABLE[target][0])["datetime"]
    return str(dt.min()), str(dt.max())


def get_mock_data(target: str, start: str = "2026-01-01", end: str = "2026-03-01") -> pd.DataFrame:
    """target("demand"/"solar"/"wind")에 맞는 데이터를 한 번에 가져오는 진입점.

    이름은 "mock"이지만 실제 데이터를 우선 쓰고, 실데이터가 부족할 때만
    목업으로 폴백한다 (기존 호출부 코드를 안 바꾸려고 함수명은 유지).
    """
    if target == "demand":
        return get_demand(start, end)
    if target in _RENEWABLE:
        return _get_renewable(target, start, end)
    raise ValueError(f"알 수 없는 target: {target}")


# 새 이름으로도 쓸 수 있게 별칭 제공
get_data = get_mock_data


if __name__ == "__main__":
    print(f"DATA_DIR = {DATA_DIR}\n")
    for t in ["demand", "solar", "wind"]:
        start, end = available_range(t)
        df = get_data(t, start=start, end=end)
        print("=" * 60)
        print(f"target = {t}  ({start} ~ {end})  행 수: {len(df)}")
        print(f"컬럼: {list(df.columns)}")
        print(f"결측: {int(df.isna().sum().sum())}\n")
