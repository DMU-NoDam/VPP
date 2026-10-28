"""dispatch_api — 급전 최적화 API (:8002).

라우트
    GET  /health
    POST /api/v1/scenarios      불확실성 시나리오 생성 (formulation.md 7절)
    POST /api/v1/dispatch/milp  UC + 경제급전 MILP (formulation.md 4장)
    POST /api/v1/dispatch/stochastic         Two-Stage + VSS (formulation.md 8~9절)
    GET  /api/v1/dispatch/stochastic/latest  대시보드용 미리 계산 결과 (precompute.py)

ESS·탄소 전후 비교·위기 시나리오는 추후 추가한다.
route 는 검증·호출·직렬화만 한다. 로직은 service.py 와 그 아래 모듈에 있다.
"""

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

import precompute
import service
import stochastic
from loaders.config_loader import ConfigError
from loaders.data_loader import DataError
from milp import SolverUnavailableError
from scenarios import generate, with_defaults
from shared.constants import API_PREFIX
from shared.schemas import (
    MilpRequest,
    MilpResponse,
    ScenarioRequest,
    ScenarioSetResponse,
    SolverOptions,
    StochasticLatest,
    StochasticRequest,
    StochasticResponse,
)
from units import load_units

logging.basicConfig(level=logging.INFO)

# config/stochastic.yaml 이 없으면 scenarios 를 불러올 때 이미 FileNotFoundError 로 멈춘다.
# 숫자 파라미터를 코드 기본값으로 대신하지 않는다 (CLAUDE.md 규칙 1·3).
_PRECOMPUTE = precompute.LatestStore(precompute.load_config())


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """서버가 뜰 때 VSS 미리 계산 스레드를 띄우고, 내려갈 때 멈춘다."""
    stop = threading.Event()
    precompute.start_background(_PRECOMPUTE, stop)
    yield
    stop.set()


app = FastAPI(title="VPP Dispatch API", version="0.2.0", lifespan=lifespan)


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


@app.post(f"{API_PREFIX}/dispatch/stochastic", response_model=StochasticResponse)
def dispatch_stochastic(req: StochasticRequest) -> StochasticResponse:
    """Two-Stage Stochastic 급전 계획과 VSS(= EEV - RP)를 돌려준다."""
    scenarios = req.scenarios
    if scenarios is None:
        # 비운 seed·n_scenarios 는 config 의 scenario_generator 값 (seed=0 도 그대로 쓴다)
        gen = with_defaults(
            ScenarioRequest(n_scenarios=req.n_scenarios, seed=req.seed, base=req.base)
        )
        scenarios = generate(gen).scenarios
    if len({len(s.demand_mw) for s in scenarios}) != 1:
        raise HTTPException(422, "시나리오 길이가 서로 다르다")
    d = stochastic.load_request_defaults()
    try:
        units = load_units(ids=req.generator_ids)
        return stochastic.run(
            units,
            scenarios,
            stochastic.load_params(),
            start=req.start,
            reserve_ratio=d.reserve_ratio if req.reserve_ratio is None else req.reserve_ratio,
            shed_penalty=(
                d.shed_penalty_won_per_mwh
                if req.shed_penalty_won_per_mwh is None
                else req.shed_penalty_won_per_mwh
            ),
            opts=req.solver or SolverOptions(time_limit_s=d.time_limit_s, mip_gap=d.mip_gap),
            compute_ws=req.compute_ws,
            include_scenario_hours=req.include_scenario_hours,
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(422, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(500, str(e)) from e


@app.get(f"{API_PREFIX}/dispatch/stochastic/latest", response_model=StochasticLatest)
def dispatch_stochastic_latest() -> StochasticLatest:
    """서버가 미리 계산해 둔 마지막 Two-Stage 결과 (대시보드 VSS 타일용). 계산 조건 포함."""
    return _PRECOMPUTE.snapshot()
