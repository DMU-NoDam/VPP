"""forecast_api 진입점. /health + POST /api/v1/forecast 제공."""

from fastapi import FastAPI

from router import router

app = FastAPI(title="VPP Forecast API", version="0.1.0")
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
