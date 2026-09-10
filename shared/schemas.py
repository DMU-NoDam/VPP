"""공용 pydantic 스키마 껍데기 — 필드 정의만, 검증 로직 없음."""

from datetime import datetime

from pydantic import BaseModel


class ForecastRequest(BaseModel):
    target_date: datetime
    horizon_h: int
    region: str | None = None


class ForecastPoint(BaseModel):
    timestamp: datetime
    demand_mw: float
    solar_mw: float
    wind_mw: float


class ForecastResponse(BaseModel):
    model_version: str
    points: list[ForecastPoint]
