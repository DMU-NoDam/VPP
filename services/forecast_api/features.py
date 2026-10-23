"""
Feature Engineering (재료 손질)

data_client.py가 주는 원본 표(datetime + 정답 컬럼 [+ 날씨 컬럼])에
모델이 패턴을 배울 수 있는 힌트 컬럼들을 추가한다.

- 시간 힌트: hour, dayofweek, is_weekend
- 과거값 힌트(lag): 1시간/24시간/168시간 전 값
- 추세 힌트(rolling): 최근 24시간 평균

태양광/풍력에 이미 붙어오는 irradiance, wind_speed 같은 날씨 컬럼은
그 자체로 좋은 힌트라서 별도 가공 없이 그대로 둔다.
"""

from __future__ import annotations

import pandas as pd


def add_time_features(df: pd.DataFrame) -> pd.DataFrame:
    """지금이 몇 시, 무슨 요일, 주말인지 컬럼 추가."""
    df = df.copy()
    df["hour"] = df["datetime"].dt.hour
    df["dayofweek"] = df["datetime"].dt.dayofweek
    df["is_weekend"] = df["dayofweek"].isin([5, 6]).astype(int)
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


def build_features(df: pd.DataFrame, target_col: str) -> pd.DataFrame:
    """위 세 가지 손질을 순서대로 적용하는 조립 함수.

    target_col: 정답 컬럼명 ("demand_mw" / "solar_mw" / "wind_mw")
    태양광의 irradiance, 풍력의 wind_speed처럼 이미 붙어있는
    날씨 컬럼은 그대로 유지된다 (df.copy() 기반이라 자동으로 남음).
    """
    df = df.sort_values("datetime").reset_index(drop=True)
    df = add_time_features(df)
    df = add_lag_features(df, target_col)
    df = add_rolling_features(df, target_col)
    return df.dropna().reset_index(drop=True)


if __name__ == "__main__":
    from data_client import get_mock_data

    target_map = {"demand": "demand_mw", "solar": "solar_mw", "wind": "wind_mw"}

    for target, target_col in target_map.items():
        print("=" * 60)
        print(f"target = {target}  (target_col = {target_col})")
        print("=" * 60)
        raw = get_mock_data(target, start="2026-01-01", end="2026-01-10")
        featured = build_features(raw, target_col)
        print(f"손질 전 컬럼: {list(raw.columns)}")
        print(f"손질 후 컬럼: {list(featured.columns)}")
        print(featured.head(3))
        print(f"손질 전 행 수: {len(raw)} -> 손질 후 행 수: {len(featured)}\n")
