"""
데이터 접근 계층 (Data Access Layer)

지금은 A(collector)의 실제 데이터 API 스키마가 확정되기 전이라
목업(synthetic) 데이터로 개발한다. 나중에 실제 데이터가 준비되면
get_demand / get_solar / get_wind 내부 구현만 실제 API 호출로
교체하면 되고, 이 함수들을 호출하는 다른 코드(features.py,
train_lgbm.py 등)는 전혀 수정할 필요가 없도록 설계했다.

target별로 반환되는 DataFrame은 항상 아래 형태를 따른다.
- demand : columns = ["datetime", "demand_mw"]
- solar  : columns = ["datetime", "solar_mw", "irradiance"]
- wind   : columns = ["datetime", "wind_mw", "wind_speed"]
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# TODO: A의 데이터 API 스키마 확정되면 이 값을 실제 엔드포인트로 교체
DATA_API_BASE = "http://collector:8000"


def _hourly_index(start: str, end: str) -> pd.DatetimeIndex:
    return pd.date_range(start, end, freq="h")


def make_synthetic_demand(start: str = "2026-01-01", end: str = "2026-03-01") -> pd.DataFrame:
    """가짜 수요 이력 생성. 하루 주기 패턴 + 약한 요일 효과 + 노이즈."""
    dates = _hourly_index(start, end)
    hour = dates.hour
    dayofweek = dates.dayofweek
    n = len(dates)

    weekday_boost = np.where(dayofweek < 5, 1.0, 0.85)  # 주말엔 수요가 조금 낮음
    daily_pattern = 10000 * np.sin((hour - 6) / 24 * 2 * np.pi)
    demand = (55000 + daily_pattern) * weekday_boost + np.random.normal(0, 1500, n)

    return pd.DataFrame({"datetime": dates, "demand_mw": demand.round(1)})


def make_synthetic_solar(start: str = "2026-01-01", end: str = "2026-03-01") -> pd.DataFrame:
    """가짜 태양광 발전량 + 일사량 생성. 낮 시간대에만 값이 생기는 패턴."""
    dates = _hourly_index(start, end)
    hour = dates.hour
    n = len(dates)

    # 6시~18시 사이에만 해가 떠 있다고 가정, 정오에 최대
    daylight = np.clip(np.sin((hour - 6) / 12 * np.pi), 0, None)
    irradiance = daylight * 850 + np.random.normal(0, 25, n)
    irradiance = np.clip(irradiance, 0, None)

    solar_mw = irradiance * 12 + np.random.normal(0, 100, n)
    solar_mw = np.clip(solar_mw, 0, None)

    return pd.DataFrame({
        "datetime": dates,
        "solar_mw": solar_mw.round(1),
        "irradiance": irradiance.round(1),
    })


def make_synthetic_wind(start: str = "2026-01-01", end: str = "2026-03-01") -> pd.DataFrame:
    """가짜 풍력 발전량 + 풍속 생성. 풍속-출력 관계를 단순 파워커브로 근사."""
    dates = _hourly_index(start, end)
    n = len(dates)

    wind_speed = np.random.gamma(shape=2.0, scale=3.0, size=n)  # 평균 6m/s 근방
    wind_speed = np.clip(wind_speed, 0, 25)

    # 파워커브 근사: 3m/s 이하 발전 없음, 12m/s 근처에서 정격 출력, 25m/s 넘으면 정지
    wind_mw = np.where(
        wind_speed < 3, 0,
        np.where(wind_speed < 12, (wind_speed - 3) ** 2 * 8, 700)
    )
    wind_mw = np.where(wind_speed >= 25, 0, wind_mw)
    wind_mw = wind_mw + np.random.normal(0, 20, n)
    wind_mw = np.clip(wind_mw, 0, None)

    return pd.DataFrame({
        "datetime": dates,
        "wind_mw": wind_mw.round(1),
        "wind_speed": wind_speed.round(2),
    })


def get_demand(start: str, end: str) -> pd.DataFrame:
    """수요 이력 조회. 지금은 목업, 추후 실제 데이터 API 호출로 교체 예정."""
    # TODO: requests.get(f"{DATA_API_BASE}/data/demand", params={"start": start, "end": end})
    return make_synthetic_demand(start, end)


def get_solar(start: str, end: str) -> pd.DataFrame:
    """태양광 발전량 + 일사량 조회. 지금은 목업."""
    # TODO: requests.get(f"{DATA_API_BASE}/data/solar", params={"start": start, "end": end})
    return make_synthetic_solar(start, end)


def get_wind(start: str, end: str) -> pd.DataFrame:
    """풍력 발전량 + 풍속 조회. 지금은 목업."""
    # TODO: requests.get(f"{DATA_API_BASE}/data/wind", params={"start": start, "end": end})
    return make_synthetic_wind(start, end)


def get_mock_data(target: str, start: str = "2026-01-01", end: str = "2026-03-01") -> pd.DataFrame:
    """target("demand"/"solar"/"wind")에 맞는 데이터를 한 번에 가져오는 진입점."""
    if target == "demand":
        return get_demand(start, end)
    if target == "solar":
        return get_solar(start, end)
    if target == "wind":
        return get_wind(start, end)
    raise ValueError(f"알 수 없는 target: {target}")


if __name__ == "__main__":
    for t in ["demand", "solar", "wind"]:
        print("=" * 60)
        print(f"target = {t}")
        print("=" * 60)
        df = get_mock_data(t, start="2026-01-01", end="2026-01-03")
        print(df.head())
        print(f"행 수: {len(df)}\n")
