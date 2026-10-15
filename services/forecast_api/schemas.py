"""
forecast_api 전용 요청/응답 스키마.

주의: shared/schemas.py에도 ForecastRequest/ForecastResponse가 있는데,
필드가 다르다 (target 선택 필드·신뢰구간 필드가 없음). 그쪽은 팀 공용이라
C/D와 협의 후 맞춰야 하고, 지금 이 파일은 forecast_api 내부에서
먼저 구조를 완성하기 위한 로컬 스키마다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Target = Literal["demand", "solar", "wind", "all"]
ModelName = Literal["auto", "lightgbm", "lstm"]
PiMethod = Literal["conformal", "quantile"]


class ForecastRequest(BaseModel):
    start_date: str = Field(..., description="과거 데이터 수집 시작일 (YYYY-MM-DD)")
    end_date: str = Field(..., description="과거 데이터 수집 종료일 (YYYY-MM-DD)")
    target: Target = Field(..., description="demand / solar / wind / all")
    horizon_hours: int = Field(..., ge=24, le=168, description="예측 시간 범위 (24~168)")
    model: ModelName = "auto"
    pi_method: PiMethod = "conformal"


class ForecastPoint(BaseModel):
    datetime: datetime
    predicted: float
    lower_bound: float
    upper_bound: float


class TargetForecast(BaseModel):
    model_used: str
    pi_method: str
    metrics: dict[str, float]
    points: list[ForecastPoint]


class ForecastResponse(BaseModel):
    result_code: str = "00"
    result_msg: str = "OK"
    generated_at: datetime
    horizon_hours: int
    forecasts: dict[str, TargetForecast]


class ErrorResponse(BaseModel):
    result_code: str
    result_msg: str
    detail: str
