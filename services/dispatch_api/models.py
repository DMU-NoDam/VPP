"""dispatch_api 도메인 타입.

로더가 만들고 MILP 가 소비한다. 전부 frozen dataclass 로 두어 최적화 도중에
값이 바뀌지 않게 한다.

단위는 필드명에 붙인다 (CLAUDE.md 단위 규칙). 전력 MW, 에너지 MWh,
비용 원/MWh, 램프 MW/h, 배출 tCO2/MWh, time step 1시간.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class GeneratorSpec:
    """generators.yaml 한 유닛. 연료비가 아직 결합되지 않은 상태.

    `fuel_cost_krw_per_mwh` 는 yaml 에 명시된 override 만 담는다 (수력 0 처럼
    fuel_cost 데이터셋에 없는 발전원). None 이면 fuel 코드로 조인해야 한다.
    """

    id: str
    fuel: str
    p_max_mw: float
    p_min_mw: float
    ramp_up_mw_per_h: float
    ramp_down_mw_per_h: float
    min_up_h: int
    min_down_h: int
    startup_cost_krw: float
    co2_ton_per_mwh: float
    u_init: int
    init_state_h: int
    fuel_cost_krw_per_mwh: float | None = None


@dataclass(frozen=True)
class Generator:
    """연료비까지 결합된 유닛. MILP 가 받는 최종 형태.

    GeneratorSpec 과 달리 `fuel_cost_krw_per_mwh` 가 필수다. 타입만 보고도
    "연료비가 해결된 상태"임을 알 수 있게 두 타입을 나눴다.
    """

    id: str
    fuel: str
    p_max_mw: float
    p_min_mw: float
    ramp_up_mw_per_h: float
    ramp_down_mw_per_h: float
    min_up_h: int
    min_down_h: int
    startup_cost_krw: float
    co2_ton_per_mwh: float
    u_init: int
    init_state_h: int
    fuel_cost_krw_per_mwh: float


@dataclass(frozen=True)
class Settings:
    """config/settings.yaml 운영 파라미터."""

    horizon_h: int
    time_step_min: int
    timezone: str
    solver: str
    solver_msg: bool
    solver_time_limit_s: int | None
    solver_mip_gap: float
    reserve_margin_ratio: float
    resample: str
    min_sample_ratio: float
    net_demand_mode: str
    fuel_cost_month: str
    hydro_threshold_pct: float = 30.0
    hydro_min_factor: float = 0.5
    hydro_derate_mode: str = "linear"
    shed_penalty_krw_per_mwh: float = 10_000_000.0

    @property
    def hydro_policy(self) -> HydroPolicy:
        """저수율 제한 정책. settings.yaml 의 hydro_* 필드에서 만든다."""
        return HydroPolicy(
            threshold_pct=self.hydro_threshold_pct,
            min_factor=self.hydro_min_factor,
            mode=self.hydro_derate_mode,
        )


@dataclass(frozen=True)
class DispatchInput:
    """MILP 한 번 풀기에 필요한 입력 전부.

    `timestamps` 와 `net_demand_mw` 는 길이가 같고 1시간 간격이다.
    D 의 Two-Stage Stochastic 이 같은 타입을 재사용할 수 있도록 시나리오별
    재실행은 net_demand_mw 만 바꿔 다시 넣는 방식을 의도했다.
    """

    generators: list[Generator]
    timestamps: list[datetime]
    net_demand_mw: list[float]
    reserve_margin_ratio: float
    storage_rate_pct: float | None = None
    p_init_mw: dict[str, float] | None = None
    """유닛별 직전 시간(t=-1) 출력. 램프 제약의 초기 조건이다.

    [미정] 수집 데이터에는 유닛별 실측 출력이 없다 (발전원별 합계뿐). None 이면
    MILP·baseline 이 t=0 의 램프 제약을 걸지 않고, 그 사실을 결과의 notes 에
    남긴다. 모르는 값을 Pmin 으로 가정하면 첫 시간이 인위적으로 묶여
    없는 infeasible 이 생긴다.
    """

    @property
    def horizon_h(self) -> int:
        return len(self.timestamps)


@dataclass(frozen=True)
class EssSpec:
    """config/ess.yaml 의 ESS 사양.

    규모와 효율은 과제가 고정하지 않은 `[미정]` 값이다. 근거는 yaml 에 적는다.

    SOC 는 **MWh 로 다룬다**. 비율로 다루면 "SOC 가 0.5" 가 50% 인지 0.5 MWh 인지
    헷갈리고, 전이식에서 용량을 곱하는 걸 잊는 버그가 생긴다 (CLAUDE.md 단위 규칙).
    yaml 은 사람이 읽기 쉬운 % 로 받고, 여기서 MWh 로 환산해 노출한다.

    왕복효율은 충전·방전에 반씩 나눈다: eta_ch = eta_dis = sqrt(rte).
    """

    power_charge_mw: float
    power_discharge_mw: float
    capacity_mwh: float
    round_trip_efficiency: float
    soc_min_pct: float
    soc_max_pct: float
    soc_init_pct: float
    soc_final_min_pct: float
    enforce_exclusive: bool = False
    mode: str = "cost"               # cost | peak_shaving | combined
    peak_target_pct: float = 5.0     # peak_shaving 목표 감소율
    peak_penalty_krw_per_mw: float = 0.0  # combined 모드의 피크 1 MW 가중치

    @property
    def eta_charge(self) -> float:
        return math.sqrt(self.round_trip_efficiency)

    @property
    def eta_discharge(self) -> float:
        return math.sqrt(self.round_trip_efficiency)

    @property
    def soc_min_mwh(self) -> float:
        return self.capacity_mwh * self.soc_min_pct / 100.0

    @property
    def soc_max_mwh(self) -> float:
        return self.capacity_mwh * self.soc_max_pct / 100.0

    @property
    def soc_init_mwh(self) -> float:
        return self.capacity_mwh * self.soc_init_pct / 100.0

    @property
    def soc_final_min_mwh(self) -> float:
        """종료 SOC 하한. 이게 없으면 배터리를 비우고 끝내 '공짜 에너지'를 얻는다."""
        return self.capacity_mwh * self.soc_final_min_pct / 100.0

    @property
    def usable_mwh(self) -> float:
        return self.soc_max_mwh - self.soc_min_mwh


@dataclass(frozen=True)
class EssSchedule:
    """ESS 운전 계획. 리스트 길이는 horizon_h."""

    charge_mw: list[float]
    discharge_mw: list[float]
    soc_mwh: list[float]
    spec: EssSpec

    def net_mw(self, t: int) -> float:
        """계통 기준 순출력 (+ 방전 / - 충전)."""
        return self.discharge_mw[t] - self.charge_mw[t]

    @property
    def energy_charged_mwh(self) -> float:
        return sum(self.charge_mw)

    @property
    def energy_discharged_mwh(self) -> float:
        return sum(self.discharge_mw)

    @property
    def loss_mwh(self) -> float:
        """왕복효율 손실. 충전량 - 방전량 - SOC 증가분."""
        soc_gain = self.soc_mwh[-1] - self.spec.soc_init_mwh if self.soc_mwh else 0.0
        return self.energy_charged_mwh - self.energy_discharged_mwh - soc_gain

    @property
    def cycles(self) -> float:
        """사용 가능 에너지 기준 등가 사이클 수."""
        usable = self.spec.usable_mwh
        return self.energy_discharged_mwh / usable if usable > 0 else 0.0


@dataclass(frozen=True)
class HydroPolicy:
    """저수율에 따른 수력 출력 제한 정책 (docs/formulation.md 4.2-6).

    [미정] 과제는 임계값 30% 만 지정하고 "얼마나 제한할지"는 정하지 않았다.
    아래는 제안이다: 저수율이 threshold 미만이면 수력 유닛의 p_max 에
    factor(rate) 를 곱한다.

        linear : factor = min_factor + (1 - min_factor) * rate / threshold
                 threshold 에서 1.0, 저수율 0% 에서 min_factor 로 선형 감소
        fixed  : factor = min_factor (임계값 미만이면 일괄 적용)

    linear 을 기본으로 둔 이유는 30.0% 와 29.9% 사이에서 급전 계획이 계단처럼
    바뀌지 않게 하려는 것이다 (해의 안정성).
    """

    threshold_pct: float = 30.0
    min_factor: float = 0.5
    mode: str = "linear"

    def factor(self, storage_rate_pct: float | None) -> float:
        """수력 p_max 에 곱할 계수. 저수율을 모르면(None) 제한하지 않는다."""
        if storage_rate_pct is None or storage_rate_pct >= self.threshold_pct:
            return 1.0
        rate = max(storage_rate_pct, 0.0)
        if self.mode == "fixed":
            return self.min_factor
        return self.min_factor + (1.0 - self.min_factor) * rate / self.threshold_pct


@dataclass(frozen=True)
class UnitSchedule:
    """유닛 하나의 horizon 전체 계획. 리스트 길이는 모두 horizon_h 다."""

    unit_id: str
    fuel: str
    on: list[bool]
    p_mw: list[float]
    startup: list[bool]
    shutdown: list[bool]
    p_max_eff_mw: float      # 수력 제한 적용 후 상한 (제한 없으면 p_max_mw 와 같다)
    fuel_cost_krw_per_mwh: float
    startup_cost_krw: float
    co2_ton_per_mwh: float

    @property
    def energy_mwh(self) -> float:
        """time step 1시간이므로 출력 합이 곧 에너지다."""
        return sum(self.p_mw)

    @property
    def n_startups(self) -> int:
        return sum(self.startup)


@dataclass(frozen=True)
class ModelSize:
    """문제 크기와 시간. 10초 KPI 를 추적하려면 매 실행마다 남겨야 한다."""

    n_variables: int
    n_binaries: int
    n_constraints: int
    build_time_s: float
    solve_time_s: float

    @property
    def total_time_s(self) -> float:
        return self.build_time_s + self.solve_time_s


@dataclass(frozen=True)
class DispatchResult:
    """급전 계획 한 개. MILP 와 Rule-based baseline 이 같은 타입을 돌려준다.

    같은 타입이어야 비용·제약 검증·탄소 계산을 같은 코드로 돌릴 수 있다
    (CLAUDE.local.md: 같은 cost accounting 으로 비교한다).
    """

    method: str                  # "milp" | "rule_based"
    status: str                  # optimal | feasible | infeasible | time_limit | error
    timestamps: list[datetime]
    net_demand_mw: list[float]
    units: list[UnitSchedule]
    shed_mw: list[float]
    fuel_cost_krw: float
    startup_cost_krw: float
    shed_cost_krw: float
    reserve_margin_ratio: float
    hydro_factor: float
    size: ModelSize | None = None
    notes: list[str] = field(default_factory=list)
    ess: EssSchedule | None = None
    surplus_mw: list[float] = field(default_factory=list)
    """시간대별 과잉발전 (MW). 수요보다 많이 낸 양으로, 감발·수출 대상이다.

    MILP 는 수급 균형이 등식이라 항상 0 이다. Rule-based baseline 은 lookahead 가
    없어 램프다운 한계에 걸려 못 내리는 경우가 생기고, 그때 이 값이 0 이 아니다.
    물리 제약(램프)을 깨는 대신 과잉분을 숫자로 드러낸다.
    """

    @property
    def horizon_h(self) -> int:
        return len(self.timestamps)

    @property
    def total_cost_krw(self) -> float:
        """목적함수와 같은 회계. 절감률 비교는 반드시 이 값으로 한다."""
        return self.fuel_cost_krw + self.startup_cost_krw + self.shed_cost_krw

    @property
    def operating_cost_krw(self) -> float:
        """페널티를 뺀 운영비 (연료비 + 기동비)."""
        return self.fuel_cost_krw + self.startup_cost_krw

    def supply_mw(self, t: int) -> float:
        """발전기 출력 합. ESS 방전은 포함하지 않는다."""
        return sum(u.p_mw[t] for u in self.units)

    def ess_net_mw(self, t: int) -> float:
        """ESS 순출력 (+ 방전 / - 충전). ESS 가 없으면 0."""
        return self.ess.net_mw(t) if self.ess else 0.0

    def served_demand_mw(self, t: int) -> float:
        return self.net_demand_mw[t] - self.shed_mw[t]

    def generator_load_mw(self, t: int) -> float:
        """발전기가 담당해야 하는 부하 = 공급한 수요 + 충전 - 방전.

        예비율의 분모이자 ESS 적용 후 피크의 정의다. ESS 가 방전하면 발전기가
        덜 내고, 충전하면 더 내야 한다.
        """
        return self.served_demand_mw(t) - self.ess_net_mw(t)

    def surplus_at(self, t: int) -> float:
        return self.surplus_mw[t] if t < len(self.surplus_mw) else 0.0

    def available_capacity_mw(self, t: int) -> float:
        """가동 중인 유닛의 상한 합. 예비율 분자에 쓴다."""
        return sum(u.p_max_eff_mw for u in self.units if u.on[t])

    def reserve_pct(self, t: int) -> float:
        """(가동 용량 - 발전기 부하) / 발전기 부하 x 100."""
        load = self.generator_load_mw(t)
        if load <= 0:
            return 0.0
        return (self.available_capacity_mw(t) - load) / load * 100.0

    @property
    def peak_demand_mw(self) -> float:
        """ESS 적용 전 피크 = 순수요의 최대값.

        ESS 가 없으면 발전기가 순수요를 그대로 담당하므로 이 값이 곧 피크 부하다.
        """
        return max(self.net_demand_mw) if self.net_demand_mw else 0.0

    @property
    def peak_after_ess_mw(self) -> float:
        """ESS 적용 후 피크 = max(순수요 + 충전 - 방전)."""
        if not self.timestamps:
            return 0.0
        return max(
            self.net_demand_mw[t] - self.ess_net_mw(t) for t in range(self.horizon_h)
        )

    @property
    def peak_reduction_pct(self) -> float:
        """피크 감소율 = (전 - 후) / 전 x 100. KPI 기준 >= 5%.

        분모는 ESS 미적용 순수요 피크다 (docs/formulation.md 5.4).
        """
        before = self.peak_demand_mw
        if before <= 0:
            return 0.0
        return (before - self.peak_after_ess_mw) / before * 100.0
