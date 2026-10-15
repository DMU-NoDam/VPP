"""공용 pydantic 스키마 껍데기 — 필드 정의만, 검증 로직 없음."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


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


# --- 급전 공통 (초안: /dispatch/milp 와 같이 쓰려는 조각, C 와 맞춰야 함) ------------

SolveStatus = Literal["optimal", "feasible", "infeasible", "time_limit", "error"]


class SolverOptions(BaseModel):
    time_limit_s: float = 10.0
    mip_gap: float = 0.005


class UnitHour(BaseModel):
    """유닛 1개 x 1시간 급전값."""

    timestamp: datetime
    unit_id: str      # generators.yaml 의 id
    on: bool          # 기동 여부 u[g,t]
    p_mw: float       # 출력


class HourSummary(BaseModel):
    """1시간 수급 요약. 대시보드 게이지·알람이 이걸 본다."""

    timestamp: datetime
    demand_mw: float
    supply_mw: float
    reserve_pct: float   # (가용 용량 - 수요) / 수요 * 100
    shed_mw: float       # 부하차단 슬랙. 0 이 아니면 용량 부족
    ess_mw: float        # + 방전 / - 충전
    co2_t: float


class CostBreakdown(BaseModel):
    fuel_won: float
    startup_won: float
    recourse_won: float  # 2단계 조정 비용 (MILP 에서는 0)
    shed_won: float      # 부하차단 페널티
    total_won: float


# --- POST /api/v1/dispatch/stochastic (초안) -------------------------------------


class Scenario(BaseModel):
    """불확실성 시나리오 1개. 리스트 길이는 horizon_h."""

    scenario_id: str
    probability: float
    demand_mw: list[float]
    solar_mw: list[float]
    wind_mw: list[float]


# --- POST /api/v1/scenarios (시나리오 생성기) --------------------------------------


class ScenarioProfile(BaseModel):
    """24h 기준 예측. 리스트 길이 = horizon."""

    demand_mw: list[float]
    solar_mw: list[float]
    wind_mw: list[float]


class ScenarioRequest(BaseModel):
    n_scenarios: int = Field(10, ge=1, le=50)      # 축약 후 개수 (요구사항: 10개 이상)
    n_samples: int = Field(300, ge=50, le=2000)    # 축약 전 표본 수 (거리 행렬이 n² 이라 상한)
    seed: int = 42
    demand_sigma_pct: float = 2.5   # 수요 오차 표준편차
    demand_ar: float = 0.9          # 수요 오차 시간 상관 (AR(1) 계수)
    solar_cloud_pct: float = 10.0   # 하루 흐림 계수의 평균 감소폭 (클수록 하방 꼬리가 두꺼움)
    wind_sigma_pct: float = 25.0    # 풍력 로그정규 오차
    solar_capacity_mw: float = 12000.0
    wind_capacity_mw: float = 3500.0
    base: ScenarioProfile | None = None  # None 이면 기본 예측 (forecast_api 연동 전)


class ScenarioDetail(Scenario):
    label: str                 # 사후 라벨. 예: "고수요·흐림"
    peak_net_load_mw: float    # 순부하 = 수요 - 태양광 - 풍력
    demand_dev_pct: float      # 기준 예측 대비 일간 에너지 편차
    solar_dev_pct: float
    wind_dev_pct: float


class Band(BaseModel):
    """축약 전 표본의 시간별 분위수."""

    p5: list[float]
    p50: list[float]
    p95: list[float]


class ScenarioCheck(BaseModel):
    name: str
    ok: bool
    detail: str


class ScenarioSetResponse(BaseModel):
    horizon_h: int
    n_samples: int
    seed: int
    base: ScenarioProfile
    scenarios: list[ScenarioDetail]   # 순부하 피크 내림차순
    net_load_band: Band
    demand_band: Band
    checks: list[ScenarioCheck]


class StochasticRequest(BaseModel):
    start: datetime
    horizon_h: int = 24
    # scenarios 를 직접 주거나, 비우면 dispatch_api 가 예측+잔차로 n_scenarios 개 만든다.
    scenarios: list[Scenario] | None = None
    n_scenarios: int = 10
    seed: int | None = None
    generator_ids: list[str] | None = None  # None 이면 generators.yaml 전체
    use_ess: bool = True
    reserve_ratio: float = 0.10
    shed_penalty_won_per_mwh: float = 10_000_000.0  # VOLL 대용. 값은 formulation.md 에서 확정
    include_scenario_hours: bool = False  # True 면 시나리오별 시간 요약까지 반환 (응답 커짐)
    solver: SolverOptions = Field(default_factory=SolverOptions)


class ScenarioResult(BaseModel):
    """1단계 해를 고정했을 때 시나리오 하나의 2단계 결과."""

    scenario_id: str
    probability: float
    cost: CostBreakdown
    shed_mwh: float
    rt_adjust_mwh: float  # 1단계 계획 대비 실시간 조정량 |p_rt - p_da| 합
    hours: list[HourSummary] | None = None


class VssReport(BaseModel):
    """VSS = EEV - RP. 기준은 vss_pct >= 3."""

    rp_won: float                   # Recourse Problem: Stochastic 해의 기대비용
    ev_won: float                   # Expected Value 문제(평균 시나리오 1개) 목적값
    eev_won: float                  # EV 1단계 해 고정 후 전 시나리오 2단계 기대비용
    vss_won: float
    vss_pct: float                  # vss_won / eev_won * 100
    ws_won: float | None = None     # Wait-and-See (선택)
    evpi_won: float | None = None   # rp - ws (선택)
    target_met: bool


class StochasticResponse(BaseModel):
    status: SolveStatus
    solve_time_s: float
    start: datetime
    horizon_h: int
    n_scenarios: int
    first_stage: list[UnitHour]           # 시나리오 공통: 기동계획 + Day-ahead 출력
    expected_cost: CostBreakdown          # 확률 가중 기대비용 (= RP)
    expected_hours: list[HourSummary]     # 확률 가중 평균 수급
    scenarios: list[ScenarioResult]
    vss: VssReport
