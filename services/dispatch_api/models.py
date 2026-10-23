"""dispatch_api 도메인 타입.

로더가 만들고 MILP 가 소비한다. 전부 frozen dataclass 로 두어 최적화 도중에
값이 바뀌지 않게 한다.

단위는 필드명에 붙인다 (CLAUDE.md 단위 규칙). 전력 MW, 에너지 MWh,
비용 원/MWh, 램프 MW/h, 배출 tCO2/MWh, time step 1시간.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    net_demand_mode: str
    fuel_cost_month: str


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

    @property
    def horizon_h(self) -> int:
        return len(self.timestamps)
