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
    p_mw: float       # 출력 (stochastic 의 first_stage 에서는 시나리오 기대 출력)


class HourSummary(BaseModel):
    """1시간 수급 요약. 대시보드 게이지·알람이 이걸 본다."""

    timestamp: datetime
    demand_mw: float
    supply_mw: float
    reserve_pct: float   # (가용 용량 - 수요) / 수요 * 100
    shed_mw: float       # 부하차단 슬랙. 0 이 아니면 용량 부족
    ess_mw: float        # + 방전 / - 충전
    co2_t: float


class CheckItem(BaseModel):
    """검증 1건. 제약 위반 검사와 시나리오 검증이 같이 쓴다."""

    name: str
    ok: bool
    detail: str


class CostBreakdown(BaseModel):
    fuel_won: float
    startup_won: float
    recourse_won: float  # 예비력 부족 페널티 (가정 항. 부족이 없으면 0, MILP 에서는 0)
    shed_won: float      # 부하차단 페널티
    total_won: float


# --- POST /api/v1/dispatch/stochastic ------------------------------------------


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
    """None 인 필드는 dispatch_api 가 config/stochastic.yaml 의 scenario_generator 값으로 채운다."""

    n_scenarios: int | None = Field(None, ge=1, le=50)   # 축약 후 개수 (요구사항: 10개 이상)
    n_samples: int | None = Field(None, ge=50, le=2000)  # 축약 전 표본 수 (거리 행렬 n² 상한)
    seed: int | None = None
    demand_sigma_pct: float | None = None   # 수요 오차 표준편차
    demand_ar: float | None = None          # 수요 오차 시간 상관 (AR(1) 계수)
    solar_cloud_pct: float | None = None    # 하루 흐림 계수의 평균 감소폭 (클수록 하방 꼬리↑)
    wind_sigma_pct: float | None = None     # 풍력 로그정규 오차
    solar_capacity_mw: float | None = None
    wind_capacity_mw: float | None = None
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


class ScenarioCheck(CheckItem):
    """시나리오 생성 검증 1건. 공용 CheckItem 과 모양이 같다 (이름은 호환 유지)."""


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
    """None 인 숫자 필드는 dispatch_api 가 config/stochastic.yaml 값으로 채운다
    (seed·n_scenarios 는 scenario_generator, 나머지는 request_defaults)."""

    start: datetime
    horizon_h: int | None = None  # 아직 쓰지 않는다. 시나리오 배열 길이가 계획 길이다
    # scenarios 를 직접 주거나, 비우면 dispatch_api 가 예측+잔차로 n_scenarios 개 만든다.
    scenarios: list[Scenario] | None = None
    n_scenarios: int | None = None
    seed: int | None = None
    base: ScenarioProfile | None = None  # 시나리오 생성용 기준 예측. None 이면 생성기 기본값
    compute_ws: bool = False  # True 면 WS·EVPI 도 계산 (시나리오 수만큼 더 풀어 느려짐)
    generator_ids: list[str] | None = None  # None 이면 generators.yaml 전체
    use_ess: bool = True
    reserve_ratio: float | None = None
    shed_penalty_won_per_mwh: float | None = None  # K (VOLL 대용). 값은 formulation.md 에서 확정
    include_scenario_hours: bool = False  # True 면 시나리오별 시간 요약까지 반환 (응답 커짐)
    solver: SolverOptions | None = None


class ScenarioResult(BaseModel):
    """1단계 해를 고정했을 때 시나리오 하나의 2단계 결과."""

    scenario_id: str
    probability: float
    cost: CostBreakdown
    shed_mwh: float
    rt_adjust_mwh: float  # 기대 출력 대비 이 시나리오 출력의 편차 |P_s - E[P]| 합
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


class StochasticLatest(BaseModel):
    """GET /dispatch/stochastic/latest. 서버가 미리 계산해 둔 마지막 Two-Stage 결과.

    state: pending(아직 시작 전) / running(계산 중, 이전 결과가 있으면 result 에 남아 있다)
           / done / error / disabled
    """

    state: Literal["pending", "running", "done", "error", "disabled"]
    computed_at: datetime | None = None
    seed: int
    n_scenarios: int
    reserve_ratio: float
    shed_penalty_won_per_mwh: float
    time_limit_s: float
    mip_gap: float
    result: StochasticResponse | None = None
    error: str | None = None


# --- POST /api/v1/dispatch/milp -------------------------------------------------
# 정식화는 docs/formulation.md 4장, 예시 JSON 은 docs/api_spec.md.


class MilpRequest(BaseModel):
    """모든 필드에 기본값이 있어 `{}` 만 보내도 된다.

    수요는 두 경로로 들어온다.
      - `net_demand_mw` 를 주면 그 값을 쓴다 (forecast_api 연동·시나리오 재실행)
      - 비우면 수집 데이터의 **최신 연속 구간** horizon_h 시간을 쓴다
    """

    horizon_h: int = Field(24, ge=1, le=168)
    net_demand_mw: list[float] | None = None   # 순수요 = 수요 - (태양광+풍력+신재생)
    start: datetime | None = None              # net_demand_mw 의 첫 시각 (없으면 데이터 기준)
    reserve_ratio: float | None = Field(None, ge=0, le=1)      # None 이면 settings.yaml
    storage_rate_pct: float | None = Field(None, ge=0, le=100)  # None 이면 dam_status 최신값
    p_init_mw: dict[str, float] | None = None  # 유닛별 직전 출력. 없으면 t=0 램프 제약 생략
    unit_ids: list[str] | None = None          # None 이면 generators.yaml 전체
    allow_load_shedding: bool = False          # [모델링 선택] 과제 필수 아님
    shed_penalty_won_per_mwh: float | None = None
    use_ess: bool = True                       # false 면 ESS 없이 푼다 (전/후 비교용)
    ess_mode: Literal["cost", "peak_shaving", "combined"] | None = None  # None 이면 ess.yaml
    ess_peak_target_pct: float | None = Field(None, ge=0, lt=100)  # peak_shaving 목표
    include_baseline: bool = True              # Rule-based 비교와 절감률을 같이 계산
    include_units: bool = True                 # 유닛 x 시간 급전값 (응답이 커진다)
    solver: SolverOptions | None = None


class ModelSizeOut(BaseModel):
    """문제 크기와 소요 시간. 10초 KPI 를 응답에서 바로 확인한다."""

    n_variables: int
    n_binaries: int
    n_constraints: int
    build_time_s: float
    solve_time_s: float
    total_time_s: float


class MeritOrderEntry(BaseModel):
    """발전원별 급전 순위와 투입량. Merit Order Stack 시각화용."""

    rank: int
    fuel: str
    fuel_cost_krw_per_mwh: float
    capacity_mw: float
    energy_mwh: float
    unit_hours_on: float
    n_units: float
    co2_ton: float


class CarbonSummary(BaseModel):
    total_ton: float
    hourly_ton: list[float]
    by_fuel_ton: dict[str, float]
    intensity_ton_per_mwh: float


class BaselineComparison(BaseModel):
    """Rule-based baseline 과의 비교. KPI 기준 cost_reduction_pct >= 10."""

    method: str
    rules: list[str]
    total_cost_won: float
    fuel_won: float
    startup_won: float
    carbon_total_ton: float
    peak_mw: float
    cost_reduction_pct: float
    target_met: bool
    constraints: list[CheckItem]


class EssSummary(BaseModel):
    """ESS 사양·운전 계획·피크 저감 효과. 대시보드의 ESS 현황 패널이 이걸 본다."""

    mode: str                      # cost | peak_shaving | combined
    power_charge_mw: float
    power_discharge_mw: float
    capacity_mwh: float
    round_trip_efficiency: float
    soc_min_mwh: float
    soc_max_mwh: float
    soc_init_mwh: float

    charge_mw: list[float]         # 시간대별 충전 출력
    discharge_mw: list[float]      # 시간대별 방전 출력
    soc_mwh: list[float]           # 시간대별 저장 에너지
    soc_pct: list[float]           # 같은 값을 용량 대비 %로

    energy_charged_mwh: float
    energy_discharged_mwh: float
    loss_mwh: float                # 왕복효율 손실
    cycles: float                  # 사용 가능 에너지 기준 등가 사이클

    peak_before_mw: float          # ESS 미적용 순수요 피크
    peak_after_mw: float           # ESS 적용 후 발전기 부하 피크
    peak_reduction_pct: float       # (전 - 후) / 전 x 100
    peak_target_pct: float          # 기준 (기본 5%)
    target_met: bool


class MilpResponse(BaseModel):
    status: SolveStatus
    method: str = "milp"
    horizon_h: int
    timestamps: list[datetime]
    model_size: ModelSizeOut
    cost: CostBreakdown
    hours: list[HourSummary]
    units: list[UnitHour] | None = None
    merit_order: list[MeritOrderEntry]
    carbon: CarbonSummary
    ess: EssSummary | None = None         # use_ess: false 면 null
    constraints: list[CheckItem]          # 필수 제약 위반 검사 (해를 다시 검증한 결과)
    baseline: BaselineComparison | None = None
    reserve_ratio: float
    hydro_factor: float                   # 수력 상한에 곱한 계수 (1.0 = 제한 없음)
    notes: list[str]                      # 모델링 선택·가정·경고
