"""dispatch_api — 급전 최적화 API (:8002).

라우트
    GET  /health
    POST /api/v1/scenarios      불확실성 시나리오 생성 (formulation.md 7절)
    POST /api/v1/dispatch/milp  UC + 경제급전 MILP (formulation.md 4장)

ESS·탄소 전후 비교·위기 시나리오·stochastic 은 추후 추가한다.
route 는 검증·호출·직렬화만 한다. 로직은 service.py 와 그 아래 모듈에 있다.
"""

import logging

from fastapi import FastAPI, HTTPException

import service
from loaders.config_loader import ConfigError
from loaders.data_loader import DataError
from milp import SolverUnavailableError
from scenarios import generate
from shared.constants import API_PREFIX
from shared.schemas import MilpRequest, MilpResponse, ScenarioRequest, ScenarioSetResponse

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="VPP Dispatch API", version="0.2.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(f"{API_PREFIX}/scenarios", response_model=ScenarioSetResponse)
def create_scenarios(req: ScenarioRequest) -> ScenarioSetResponse:
    """수요·태양광·풍력 불확실성 시나리오를 만들어 확률과 함께 돌려준다."""
    return generate(req)


@app.post(f"{API_PREFIX}/dispatch/milp", response_model=MilpResponse)
def dispatch_milp(req: MilpRequest) -> MilpResponse:
    """UC 포함 경제급전 MILP 를 풀어 시간대별 급전 계획을 돌려준다.

    HTTP 상태는 "요청·환경이 정상이었는지"를, 응답의 `status` 는 "최적화가 어떻게
    끝났는지"를 나타낸다. infeasible 은 최적화의 정상적인 결과이므로 200 으로
    돌려주고, 원인 진단은 `notes` 와 `constraints` 에 담는다.
    """
    try:
        return service.run_milp(req)
    except DataError as exc:            # 입력 시계열이 모자라거나 모양이 틀렸다
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ConfigError as exc:          # config 파일이 규칙을 어겼다
        raise HTTPException(status_code=500, detail=f"config 오류: {exc}") from exc
    except SolverUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
