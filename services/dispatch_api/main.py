"""dispatch_api — 급전 최적화 API (:8002).

현재 라우트: /health, POST /api/v1/scenarios (시나리오 생성기).
MILP·stochastic 은 추후 추가.
"""

from fastapi import FastAPI

from scenarios import generate
from shared.constants import API_PREFIX
from shared.schemas import ScenarioRequest, ScenarioSetResponse

app = FastAPI(title="VPP Dispatch API", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(f"{API_PREFIX}/scenarios", response_model=ScenarioSetResponse)
def create_scenarios(req: ScenarioRequest) -> ScenarioSetResponse:
    """수요·태양광·풍력 불확실성 시나리오를 만들어 확률과 함께 돌려준다."""
    return generate(req)
