"""
Feature Engineering (재료 손질)

data_client.py가 주는 원본 표(datetime + 정답 컬럼 + 기상 컬럼)에
모델이 패턴을 배울 수 있는 힌트 컬럼들을 추가한다.

- 달력 힌트: hour, dayofweek, is_weekend, dayofyear, is_holiday(공휴일), is_offday(주말 또는 공휴일)
- 기온 힌트(수요): 냉방도일 CDD / 난방도일 HDD (일평균 기온 기준) + 시간별 냉·난방 도시(CDH/HDH)
- 과거값 힌트(lag, 수요): 1시간/24시간/168시간 전 값, 최근 24시간 평균
- 기상 힌트(태양광/풍력): 지점별 일사량·풍속 + 이동평균(3/6/12/24시간) + 지점 간 최대·편차

수요는 "과거값 + 기온/달력"으로 1시간씩 이어서 예측(recursive)하고,
태양광/풍력은 "기상 + 달력"만으로 각 시각을 바로 예측(direct)한다.
(태양광/풍력은 과거 발전량을 넣으면 이어서 예측할 때 오차가 쌓여서 오히려 나빠졌다)
"""

from __future__ import annotations

import holidays
import numpy as np
import pandas as pd

from data_client import TEMPERATURE_COLUMN, weather_columns

# 냉·난방도일 기준온도 (℃). 기상청 냉난방도일 기준: 냉방 24℃, 난방 18℃
CDD_BASE = 24.0
HDD_BASE = 18.0

WEATHER_ROLL_WINDOWS = (3, 6, 12, 24)

TARGET_COLUMN = {"demand": "demand_mw", "solar": "solar_mw", "wind": "wind_mw"}


def _kr_holidays(years: list[int]) -> set:
    """한국 공휴일(대체공휴일·선거일 포함) 날짜 집합."""
    return set(holidays.KR(years=years).keys())


def add_time_features(df: pd.DataFrame) -> pd.DataFrame:
    """시각/요일/주말/연중 날짜/공휴일 컬럼 추가."""
    df = df.copy()
    dt = df["datetime"]
    df["hour"] = dt.dt.hour
    df["dayofweek"] = dt.dt.dayofweek
    df["is_weekend"] = df["dayofweek"].isin([5, 6]).astype(int)
    df["dayofyear"] = dt.dt.dayofyear
    hol = _kr_holidays(sorted(dt.dt.year.unique().tolist()))
    df["is_holiday"] = dt.dt.date.isin(hol).astype(int)
    df["is_offday"] = ((df["is_weekend"] == 1) | (df["is_holiday"] == 1)).astype(int)
    return df


def add_degree_day_features(df: pd.DataFrame, temp_col: str = TEMPERATURE_COLUMN) -> pd.DataFrame:
    """냉방도일(CDD)·난방도일(HDD)과 시간별 냉·난방 도시(CDH/HDH) 추가.

    CDD = max(일평균기온 - 24, 0), HDD = max(18 - 일평균기온, 0)  (그 날짜의 모든 시간에 같은 값)
    CDH = max(기온 - 24, 0),       HDH = max(18 - 기온, 0)        (시간별)
    """
    df = df.copy()
    daily_mean = df.groupby(df["datetime"].dt.date)[temp_col].transform("mean")
    df["cdd"] = np.maximum(daily_mean - CDD_BASE, 0.0)
    df["hdd"] = np.maximum(HDD_BASE - daily_mean, 0.0)
    df["cdh"] = np.maximum(df[temp_col] - CDD_BASE, 0.0)
    df["hdh"] = np.maximum(HDD_BASE - df[temp_col], 0.0)
    return df


def add_lag_features(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """col의 1시간/24시간/168시간(1주일) 전 값을 컬럼으로 추가."""
    df = df.copy()
    df[f"{col}_lag_1h"] = df[col].shift(1)
    df[f"{col}_lag_24h"] = df[col].shift(24)
    df[f"{col}_lag_168h"] = df[col].shift(168)
    return df


def add_rolling_features(df: pd.DataFrame, col: str, window: int = 24) -> pd.DataFrame:
    """col의 최근 window시간 이동평균을 컬럼으로 추가."""
    df = df.copy()
    df[f"{col}_roll_mean_{window}h"] = df[col].rolling(window).mean()
    return df


def add_weather_features(df: pd.DataFrame, target: str) -> pd.DataFrame:
    """태양광/풍력용 기상 파생 컬럼: 지점별·평균 이동평균(가운데 정렬) + 지점 간 최대/편차.

    가운데 정렬 이동평균은 앞뒤 시간의 기상값을 쓰는데, 예측 시점에는 "예보값"이 있다고 보고 쓴다.
    """
    df = df.copy()
    cols = weather_columns(target)
    for w in WEATHER_ROLL_WINDOWS:
        rolled = df[cols].rolling(w, center=True, min_periods=1).mean()
        rolled.columns = [f"{c}_r{w}" for c in cols]
        df = pd.concat([df, rolled], axis=1)
    station_cols = cols[:-1]
    df["weather_max"] = df[station_cols].max(axis=1)
    df["weather_spread"] = df[station_cols].std(axis=1)
    return df


def feature_columns(target: str) -> list[str]:
    """target별로 모델에 넣는 컬럼 목록 (학습과 서빙이 같은 목록을 쓴다)."""
    calendar = ["hour", "dayofweek", "is_weekend", "dayofyear", "is_holiday", "is_offday"]
    if target == "demand":
        col = TARGET_COLUMN[target]
        return (calendar + [TEMPERATURE_COLUMN, "cdd", "hdd", "cdh", "hdh"]
                + [f"{col}_lag_1h", f"{col}_lag_24h", f"{col}_lag_168h", f"{col}_roll_mean_24h"])
    cols = weather_columns(target)
    rolled = [f"{c}_r{w}" for w in WEATHER_ROLL_WINDOWS for c in cols]
    return calendar + cols + rolled + ["weather_max", "weather_spread"]


def build_exog_features(df: pd.DataFrame, target: str) -> pd.DataFrame:
    """과거 발전량/수요를 쓰지 않는 피처(달력 + 기상)만 만든다.
    미래 시점에도 계산할 수 있어서 서빙 때 그대로 재사용한다."""
    df = df.sort_values("datetime").reset_index(drop=True)
    df = add_time_features(df)
    if target == "demand":
        return add_degree_day_features(df)
    return add_weather_features(df, target)


def build_features(df: pd.DataFrame, target_col: str) -> pd.DataFrame:
    """학습용 전체 피처. target_col: "demand_mw" / "solar_mw" / "wind_mw"

    수요는 lag/rolling까지 붙이고 앞쪽 168시간(lag를 못 채운 행)은 버린다.
    """
    target = target_col.removesuffix("_mw")
    df = build_exog_features(df, target)
    if target == "demand":
        df = add_lag_features(df, target_col)
        df = add_rolling_features(df, target_col)
        return df.dropna(subset=feature_columns(target)).reset_index(drop=True)
    return df.dropna(subset=[target_col]).reset_index(drop=True)


if __name__ == "__main__":
    from data_client import available_range, get_data

    for target, target_col in TARGET_COLUMN.items():
        start, end = available_range(target)
        featured = build_features(get_data(target, start, end), target_col)
        print("=" * 60)
        print(f"target = {target}  피처 {len(feature_columns(target))}개, 행 {len(featured)}")
        print(feature_columns(target))
        if target == "demand":
            print(featured[["datetime", "is_holiday", "temperature_c", "cdd", "hdd"]]
                  .query("is_holiday == 1").head(3))
