"""scripts/measure_vss_mix.py 의 순수 함수 (수요 시나리오·조합·전날 계획 묶기·순위)."""

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from shared.schemas import ScenarioProfile, UnitHour

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "measure_vss_mix.py"
_SPEC = importlib.util.spec_from_file_location("measure_vss_mix", _PATH)
mix = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mix)


def _base(demand=100.0, solar=10.0, wind=5.0, hours=24) -> ScenarioProfile:
    return ScenarioProfile(demand_mw=[demand] * hours, solar_mw=[solar] * hours,
                           wind_mw=[wind] * hours)


def test_demand_scenarios_are_ten_with_equal_probability() -> None:
    scs = mix.demand_scenarios(_base())
    assert len(scs) == 10
    assert sum(s.probability for s in scs) == pytest.approx(1.0)
    assert [s.scenario_id for s in scs] == [
        "D-10", "D-08", "D-06", "D-04", "D-02", "D+02", "D+04", "D+06", "D+08", "D+10"]


def test_demand_scenarios_move_only_demand() -> None:
    scs = {s.scenario_id: s for s in mix.demand_scenarios(_base())}
    assert scs["D+06"].demand_mw[0] == pytest.approx(106.0)
    assert scs["D-10"].demand_mw[0] == pytest.approx(90.0)
    # 태양광·풍력은 예측 그대로 — 켜고 끄는 대상이 아니라 수요에서 빼는 공급이다
    assert all(s.solar_mw[0] == 10.0 and s.wind_mw[0] == 5.0 for s in scs.values())


def test_combos_are_15_and_full_first() -> None:
    combos = mix.combos()
    assert len(combos) == 15
    assert combos[0] == ("원자력", "석탄", "LNG", "수력")
    assert len(set(combos)) == 15


def test_unit_ids_maps_coal_group_to_both_coal_fuels() -> None:
    fuel_of = {"coal_1": "bituminous_coal", "anthracite_1": "anthracite", "lng_1": "lng"}
    assert mix.unit_ids(("석탄",), fuel_of) == ["coal_1", "anthracite_1"]


def test_is_short() -> None:
    assert mix.is_short(1_816.0, 60_000.0)
    assert not mix.is_short(110_000.0, 60_000.0)


def test_group_plan_counts_units_and_sums_output_per_hour() -> None:
    t0 = datetime(2026, 10, 28)
    fuel_of = {"coal_1": "bituminous_coal", "anthracite_1": "anthracite", "lng_1": "lng"}
    fs = [UnitHour(timestamp=t0 + timedelta(hours=h), unit_id=uid, on=on, p_mw=mw)
          for h in range(2)
          for uid, on, mw in (("coal_1", True, 100.0), ("anthracite_1", h == 1, 10.0 * h),
                              ("lng_1", False, 0.0))]
    plan = mix.group_plan(fs, fuel_of, 2)
    assert plan["석탄"] == {"units": 2, "on": [1, 2], "mw": [100.0, 110.0]}
    assert plan["LNG"] == {"units": 1, "on": [0, 0], "mw": [0.0, 0.0]}
    assert plan["원자력"]["units"] == 0


def _row(combo, status="optimal", vss=None, short=False) -> dict:
    return {"combo": combo, "status": status, "vss_pct": vss, "bound_pct": None,
            "shed_mwh": 0.0, "short": short}


def test_rank_orders_by_delta_and_separates_short_and_failed() -> None:
    full = mix.combos()[0]
    rows = [_row(full, vss=3.0), _row(("A",), vss=5.0), _row(("B",), vss=1.0),
            _row(("C",), vss=9.0, short=True), _row(("D",), status="failed")]
    ranked, excluded = mix.rank(mix.summarize(rows))
    assert [r["combo"] for r in ranked] == [("A",), ("B",)]
    assert [r["delta"] for r in ranked] == [pytest.approx(2.0), pytest.approx(-2.0)]
    assert [r["combo"] for r in excluded] == [("C",), ("D",)]


def test_rank_without_baseline_is_empty() -> None:
    full = mix.combos()[0]
    rows = [_row(full, status="failed"), _row(("A",), vss=5.0)]
    ranked, _ = mix.rank(mix.summarize(rows))
    assert ranked == []


def test_group_capacity_sums_units_per_group_and_adds_renewables() -> None:
    class U:
        def __init__(self, fuel: str, p_max_mw: float) -> None:
            self.fuel, self.p_max_mw = fuel, p_max_mw

    caps = mix.group_capacity([U("bituminous_coal", 100.0), U("anthracite", 10.0), U("lng", 50.0)],
                              {"태양광": 12_000.0, "풍력": 8_000.0})
    assert caps["석탄"] == 110.0 and caps["LNG"] == 50.0 and caps["원자력"] == 0
    assert caps["태양광"] == 12_000.0


def test_mip_gap_is_tight_enough_for_ev_plan() -> None:
    # 허용오차 0.5% 로 풀면 평균 계획(EV)이 최적이 아니어서 EEV·VSS 가 크게 흔들렸다
    # (석탄+LNG+수력: 0.5% → VSS 8.28%, 0.001% → 19.43%). 조합 비교는 거의 최적 해로 한다
    assert mix.MIP_GAP <= 1e-4
