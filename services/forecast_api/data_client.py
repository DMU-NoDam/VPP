"""
데이터 접근 계층 (Data Access Layer)

data 브랜치에서 가져온 실제 CSV 스냅샷(VPP/data/csv/*.csv)을 읽어서 쓴다.
아직 3일치 정도로 기간이 짧아서(스펙은 1년 요구) 실전 학습엔 부족하지만,
파이프라인 자체는 실제 데이터 스키마 그대로 동작하도록 맞춰뒀다.
데이터가 더 쌓이면 이 파일은 손댈 필요 없이 자동으로 기간이 늘어난다.

collector의 domain.py 기준 실제 컬럼:
    power_demand.csv        : ts, demand_mw                (5분 간격)
    generation_by_fuel.csv  : ts, fuel, mw                  (5분 간격, long format)
    weather.csv              : observed_at, station_id, wind_speed_ms,
                                temperature_c, humidity_pct, solar_radiation_mj (1시간 간격)

target별로 반환되는 DataFrame은 항상 아래 형태를 따른다 (기존과 동일하게 유지).
- demand : columns = ["datetime", "demand_mw"]
- solar  : columns = ["datetime", "solar_mw", "irradiance"]
- wind   : columns = ["datetime", "wind_mw", "wind_speed"]

주의(팀 확인 필요): generation_by_fuel의 fuel 값 9종에 "wind"가 없다.
지금은 "renewable"(신재생)을 풍력 값의 임시 대체로 쓰고 있는데, 이게 풍력만을
가리키는지 다른 신재생까지 섞인 값인지 A에게 확인이 안 된 상태다 (WIND_FUEL_CODE
주석 참고). sumperfuel5m 엔드포인트를 추가하면 진짜 풍력 필드(fuelPwr9)를 받을 수
있다.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "csv"

# lag_168h(1주일 전 값) feature를 쓰려면 최소 168시간 + 여유분이 필요하다.
# 이보다 적으면 실데이터가 있어도(0행은 아니어도) 학습에 못 쓰므로 목업으로 대체한다.
MIN_HOURS_REQUIRED = 24 * 10  # 168h + 여유 72h

# TODO(확인 필요): 진짜 풍력이 아니라 "신재생"으로 대체 중. A와 sumperfuel5m 논의 후 교체.
WIND_FUEL_CODE = "renewable"
SOLAR_FUEL_CODE = "solar"
WEATHER_STATION_ID = 108  # 서울, weather.csv에 있는 유일한 지점


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
    return pd.DataFrame({"datetime": dates, "demand_mw": demand.round(1)})


def make_synthetic_solar(start: str = "2026-01-01", end: str = "2026-03-01") -> pd.DataFrame:
    dates = _hourly_index(start, end)
    hour = dates.hour
    n = len(dates)
    daylight = np.clip(np.sin((hour - 6) / 12 * np.pi), 0, None)
    irradiance = np.clip(daylight * 850 + np.random.normal(0, 25, n), 0, None)
    solar_mw = np.clip(irradiance * 12 + np.random.normal(0, 100, n), 0, None)
    return pd.DataFrame({"datetime": dates, "solar_mw": solar_mw.round(1), "irradiance": irradiance.round(1)})


def make_synthetic_wind(start: str = "2026-01-01", end: str = "2026-03-01") -> pd.DataFrame:
    dates = _hourly_index(start, end)
    n = len(dates)
    wind_speed = np.clip(np.random.gamma(shape=2.0, scale=3.0, size=n), 0, 25)
    wind_mw = np.where(wind_speed < 3, 0, np.where(wind_speed < 12, (wind_speed - 3) ** 2 * 8, 700))
    wind_mw = np.where(wind_speed >= 25, 0, wind_mw)
    wind_mw = np.clip(wind_mw + np.random.normal(0, 20, n), 0, None)
    return pd.DataFrame({"datetime": dates, "wind_mw": wind_mw.round(1), "wind_speed": wind_speed.round(2)})


# --- 실제 CSV 로딩 -----------------------------------------------------------


def _read_power_demand() -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / "power_demand.csv", parse_dates=["ts"])
    return df.rename(columns={"ts": "datetime"})


def _read_generation_by_fuel(fuel: str) -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / "generation_by_fuel.csv", parse_dates=["ts"])
    df = df[df["fuel"] == fuel][["ts", "mw"]].rename(columns={"ts": "datetime"})
    return df


def _read_weather() -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / "weather.csv", parse_dates=["observed_at"])
    df = df[df["station_id"] == WEATHER_STATION_ID].rename(columns={"observed_at": "datetime"})
    return df


def _filter_range(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    return df[(df["datetime"] >= start) & (df["datetime"] <= end)]


def get_demand(start: str, end: str) -> pd.DataFrame:
    """수요 이력 조회. 실제 CSV(5분 간격)를 1시간 평균으로 리샘플링해서 반환.

    실데이터가 있어도 MIN_HOURS_REQUIRED보다 적으면(lag_168h를 못 채우면)
    목업으로 대체한다 — "0행은 아니지만 학습엔 못 쓰는" 상황을 막기 위함.
    """
    df = _read_power_demand()
    df = _filter_range(df, start, end)
    hourly = df.set_index("datetime")["demand_mw"].resample("h").mean().reset_index()

    if len(hourly) < MIN_HOURS_REQUIRED:
        print(f"[data_client] 경고: demand 실데이터가 {len(hourly)}시간뿐 "
              f"(최소 {MIN_HOURS_REQUIRED}시간 필요) → 목업으로 대체")
        return make_synthetic_demand(start, end)

    return hourly


def get_solar(start: str, end: str) -> pd.DataFrame:
    """태양광 발전량(generation_by_fuel, fuel=solar) + 일사량(weather) 조회."""
    gen = _read_generation_by_fuel(SOLAR_FUEL_CODE)
    gen = _filter_range(gen, start, end)
    weather = _read_weather()[["datetime", "solar_radiation_mj"]]

    gen_hourly = gen.set_index("datetime")["mw"].resample("h").mean().reset_index()
    gen_hourly = gen_hourly.rename(columns={"mw": "solar_mw"})

    if len(gen_hourly) < MIN_HOURS_REQUIRED:
        print(f"[data_client] 경고: solar 실데이터가 {len(gen_hourly)}시간뿐 "
              f"(최소 {MIN_HOURS_REQUIRED}시간 필요) → 목업으로 대체")
        return make_synthetic_solar(start, end)

    merged = pd.merge(gen_hourly, weather, on="datetime", how="left")
    merged = merged.rename(columns={"solar_radiation_mj": "irradiance"})
    merged["irradiance"] = merged["irradiance"].ffill().bfill()  # 결측 시간대 보간
    return merged


def get_wind(start: str, end: str) -> pd.DataFrame:
    """풍력 발전량 조회. WIND_FUEL_CODE("renewable")를 임시 대체값으로 사용 중.

    실제 풍력 필드가 아직 없어서(도입부 주석 참고) 정확도를 신뢰하면 안 되고,
    구조 검증용으로만 쓴다. A가 sumperfuel5m을 추가하면 이 함수만 고치면 된다.
    """
    gen = _read_generation_by_fuel(WIND_FUEL_CODE)
    gen = _filter_range(gen, start, end)
    weather = _read_weather()[["datetime", "wind_speed_ms"]]

    gen_hourly = gen.set_index("datetime")["mw"].resample("h").mean().reset_index()
    gen_hourly = gen_hourly.rename(columns={"mw": "wind_mw"})

    if len(gen_hourly) < MIN_HOURS_REQUIRED:
        print(f"[data_client] 경고: wind(renewable 대체) 실데이터가 {len(gen_hourly)}시간뿐 "
              f"(최소 {MIN_HOURS_REQUIRED}시간 필요) → 목업으로 대체")
        return make_synthetic_wind(start, end)

    merged = pd.merge(gen_hourly, weather, on="datetime", how="left")
    merged = merged.rename(columns={"wind_speed_ms": "wind_speed"})
    merged["wind_speed"] = merged["wind_speed"].ffill().bfill()
    return merged


def get_mock_data(target: str, start: str = "2026-01-01", end: str = "2026-03-01") -> pd.DataFrame:
    """target("demand"/"solar"/"wind")에 맞는 데이터를 한 번에 가져오는 진입점.

    이름은 "mock"이지만 이제 실제 데이터를 우선 시도하고, 실데이터가 없을 때만
    목업으로 폴백한다 (기존 호출부 코드를 안 바꾸려고 함수명은 유지).
    """
    if target == "demand":
        return get_demand(start, end)
    if target == "solar":
        return get_solar(start, end)
    if target == "wind":
        return get_wind(start, end)
    raise ValueError(f"알 수 없는 target: {target}")


if __name__ == "__main__":
    # 실제 CSV에 들어있는 기간을 먼저 확인
    demand_raw = _read_power_demand()
    print(f"실데이터 보유 기간: {demand_raw['datetime'].min()} ~ {demand_raw['datetime'].max()}")
    print(f"총 {len(demand_raw)}행 (5분 간격)\n")

    start = str(demand_raw["datetime"].min().date())
    end = str(demand_raw["datetime"].max().date())

    for t in ["demand", "solar", "wind"]:
        print("=" * 60)
        print(f"target = {t}  (start={start}, end={end})")
        print("=" * 60)
        df = get_mock_data(t, start=start, end=end)
        print(df.head())
        print(f"행 수: {len(df)}\n")
