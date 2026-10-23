"""POST /api/v1/forecast 라우터. HTTP 요청/응답 변환만 담당하고,
실제 예측 로직은 forecast_engine에 맡긴다.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from forecast_engine import run_forecast
from schemas import ForecastRequest, ForecastResponse

router = APIRouter(prefix="/api/v1")

TARGETS = ("demand", "solar", "wind")


@router.post("/forecast", response_model=ForecastResponse)
def forecast(req: ForecastRequest) -> ForecastResponse:
    targets = TARGETS if req.target == "all" else (req.target,)

    forecasts = {}
    for target in targets:
        try:
            forecasts[target] = run_forecast(
                target=target,
                start_date=req.start_date,
                end_date=req.end_date,
                horizon_hours=req.horizon_hours,
                model_name=req.model,
                pi_method=req.pi_method,
            )
        except FileNotFoundError as e:
            raise HTTPException(
                status_code=500,
                detail={"result_code": "21", "result_msg": "MODEL_LOAD_ERROR", "detail": str(e)},
            ) from e
        except NotImplementedError as e:
            raise HTTPException(
                status_code=501,
                detail={"result_code": "21", "result_msg": "MODEL_NOT_IMPLEMENTED", "detail": str(e)},
            ) from e

    return ForecastResponse(
        generated_at=datetime.now(),
        horizon_hours=req.horizon_hours,
        forecasts=forecasts,
    )
