"""dispatch_api — 급전 최적화 API (:8002).

현재 라우트: /health, POST /api/v1/scenarios (시나리오 생성기),
POST /api/v1/dispatch/stochastic (Two-Stage + VSS),
GET /api/v1/dispatch/stochastic/latest (대시보드용 미리 계산 결과, precompute.py).
MILP 는 추후 추가.
"""

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

import precompute
import stochastic
from scenarios import generate, with_defaults
from shared.constants import API_PREFIX
from shared.schemas import (
    ScenarioRequest,
    ScenarioSetResponse,
    SolverOptions,
    StochasticLatest,
    StochasticRequest,
    StochasticResponse,
)
from units import load_units

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


app = FastAPI(title="VPP Dispatch API", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(f"{API_PREFIX}/scenarios", response_model=ScenarioSetResponse)
def create_scenarios(req: ScenarioRequest) -> ScenarioSetResponse:
    """수요·태양광·풍력 불확실성 시나리오를 만들어 확률과 함께 돌려준다."""
    return generate(req)


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
