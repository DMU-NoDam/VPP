"""VSS 계산 검증: 손으로 풀 수 있는 작은 계통에서 stochastic.run() 이 정답을 내는지 본다.

계통: 석탄(100MW, 최소 40MW, 50,000원/MWh) + 피커(50MW, 최소 10MW, 200,000원/MWh), 4시간.
시나리오: 순수요 60MW / 130MW, 확률 각 0.5 (평균 95MW). 예비율 0, 부하차단 K = 1,000,000원/MWh.

A. 피커를 당일 켤 수 없으면 (유연성 없음)
   EV 는 평균 95MW 만 보고 석탄만 켠다 → 130MW 시나리오에서 30MW 부하차단.
   EEV = 1백만(기동) + (60·4·5만 + 100·4·5만 + 30·4·백만) / 2 = 77,000,000
   RP  = 2백만(기동 둘) + (4·(50·5만 + 10·20만) + 4·(100·5만 + 30·20만)) / 2 = 33,000,000
   WS  = (1백만 + 12백만 + 2백만 + 44백만) / 2 = 29,500,000
   VSS = 44,000,000 (EEV 의 57.1%)
B. 피커를 당일 0시부터 켤 수 있으면: 당일에 대응하면 되므로 RP = EEV = WS, VSS = 0.

tests/ 에 넣고 실행: pytest tests/test_vss_toy.py -o addopts=""
"""

from datetime import datetime

import pytest

import stochastic
from shared.schemas import Scenario, SolverOptions
from units import Unit

K = 1_000_000
OPTS = SolverOptions(time_limit_s=None, mip_gap=0.0)


def unit(id_: str, fuel: str, p_max: float, p_min: float, cost: float) -> Unit:
    return Unit(id=id_, fuel=fuel, p_max_mw=p_max, p_min_mw=p_min, ramp_up_mw_per_h=p_max,
                ramp_down_mw_per_h=p_max, min_up_h=1, min_down_h=1, startup_cost_krw=1_000_000,
                co2_ton_per_mwh=0.5, u_init=0, init_state_h=24, fuel_cost_krw_per_mwh=cost)


UNITS = [unit("coal", "bituminous_coal", 100, 40, 50_000), unit("peaker", "oil", 50, 10, 200_000)]
SCENARIOS = [
    Scenario(scenario_id=f"s{i}", probability=0.5, demand_mw=[d] * 4,
             solar_mw=[0] * 4, wind_mw=[0] * 4)
    for i, d in enumerate([60.0, 130.0])
]


def solve(startup_time_h: dict) -> stochastic.VssReport:
    params = stochastic.StochasticParams(1e6, 3.0, startup_time_h=startup_time_h)
    return stochastic.run(UNITS, SCENARIOS, params, datetime(2026, 1, 1), reserve_ratio=0.0,
                          shed_penalty=K, opts=OPTS, compute_ws=True).vss


def test_inflexible_system_has_large_vss():
    v = solve({})  # 어떤 연료도 당일 기동 불가
    assert v.eev_won == pytest.approx(77_000_000)
    assert v.rp_won == pytest.approx(33_000_000)
    assert v.ws_won == pytest.approx(29_500_000)
    assert v.vss_pct == pytest.approx(44 / 77 * 100)


def test_day_of_start_removes_vss():
    v = solve({"oil": 0})  # 피커를 당일 0시부터 기동 가능
    assert v.rp_won == pytest.approx(v.ws_won)
    assert v.eev_won == pytest.approx(v.rp_won)
    assert v.vss_pct == pytest.approx(0.0, abs=1e-9)
