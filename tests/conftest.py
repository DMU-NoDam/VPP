"""테스트 공용 설정.

서비스끼리는 import 하지 않지만(CLAUDE.md), 테스트는 대상 서비스의 모듈을
직접 불러와야 한다. dispatch_api 는 collector 와 같은 flat 모듈 관례를 쓰므로
그 디렉터리를 sys.path 에 넣어준다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DISPATCH_API_DIR = REPO_ROOT / "services" / "dispatch_api"

if str(DISPATCH_API_DIR) not in sys.path:
    sys.path.insert(0, str(DISPATCH_API_DIR))


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def csv_dir() -> Path:
    return REPO_ROOT / "data" / "csv"


# ── 최적화 테스트 공용 팩토리 ────────────────────────────────────────────────
# 손계산이 가능한 작은 유닛/입력을 만든다. 기본값은 "제약이 걸리지 않는" 값이고,
# 각 테스트가 검사할 제약만 좁혀서 쓴다.


@pytest.fixture
def make_generator():
    from models import Generator

    def _make(
        gen_id: str = "g1",
        fuel: str = "lng",
        p_max_mw: float = 100.0,
        p_min_mw: float = 20.0,
        ramp_up_mw_per_h: float = 100.0,
        ramp_down_mw_per_h: float = 100.0,
        min_up_h: int = 1,
        min_down_h: int = 1,
        startup_cost_krw: float = 0.0,
        co2_ton_per_mwh: float = 0.5,
        u_init: int = 1,
        init_state_h: int = 24,
        fuel_cost_krw_per_mwh: float = 10_000.0,
    ) -> Generator:
        return Generator(
            id=gen_id,
            fuel=fuel,
            p_max_mw=p_max_mw,
            p_min_mw=p_min_mw,
            ramp_up_mw_per_h=ramp_up_mw_per_h,
            ramp_down_mw_per_h=ramp_down_mw_per_h,
            min_up_h=min_up_h,
            min_down_h=min_down_h,
            startup_cost_krw=startup_cost_krw,
            co2_ton_per_mwh=co2_ton_per_mwh,
            u_init=u_init,
            init_state_h=init_state_h,
            fuel_cost_krw_per_mwh=fuel_cost_krw_per_mwh,
        )

    return _make


@pytest.fixture
def make_input():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from models import DispatchInput

    def _make(
        generators,
        net_demand_mw,
        reserve_margin_ratio: float = 0.10,
        storage_rate_pct: float | None = None,
        p_init_mw: dict[str, float] | None = None,
        start: str = "2026-04-10 00:00:00",
    ) -> DispatchInput:
        first = datetime.fromisoformat(start).replace(tzinfo=ZoneInfo("Asia/Seoul"))
        return DispatchInput(
            generators=list(generators),
            timestamps=[first + timedelta(hours=h) for h in range(len(net_demand_mw))],
            net_demand_mw=[float(v) for v in net_demand_mw],
            reserve_margin_ratio=reserve_margin_ratio,
            storage_rate_pct=storage_rate_pct,
            p_init_mw=p_init_mw,
        )

    return _make


@pytest.fixture
def make_ess():
    """손계산이 쉬운 ESS. 왕복효율 0.64 -> eta_ch = eta_dis = 0.8 이다."""
    from models import EssSpec

    def _make(
        power_charge_mw: float = 100.0,
        power_discharge_mw: float = 100.0,
        capacity_mwh: float = 400.0,
        round_trip_efficiency: float = 0.64,
        soc_min_pct: float = 0.0,
        soc_max_pct: float = 100.0,
        soc_init_pct: float = 50.0,
        soc_final_min_pct: float = 0.0,
        enforce_exclusive: bool = False,
        mode: str = "cost",
        peak_target_pct: float = 5.0,
        peak_penalty_krw_per_mw: float = 0.0,
    ) -> EssSpec:
        return EssSpec(
            power_charge_mw=power_charge_mw,
            power_discharge_mw=power_discharge_mw,
            capacity_mwh=capacity_mwh,
            round_trip_efficiency=round_trip_efficiency,
            soc_min_pct=soc_min_pct,
            soc_max_pct=soc_max_pct,
            soc_init_pct=soc_init_pct,
            soc_final_min_pct=soc_final_min_pct,
            enforce_exclusive=enforce_exclusive,
            mode=mode,
            peak_target_pct=peak_target_pct,
            peak_penalty_krw_per_mw=peak_penalty_krw_per_mw,
        )

    return _make
