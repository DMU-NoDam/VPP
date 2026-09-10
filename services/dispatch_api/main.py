"""dispatch_api 스텁 — /health 만 제공."""

from fastapi import FastAPI

app = FastAPI(title="VPP Dispatch API", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
