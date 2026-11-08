"""탄소 배출 계산 단위 테스트. 손계산으로 검증한다."""

from __future__ import annotations

import pytest

import baseline
import carbon


def test_hourly_and_total_emissions(make_generator, make_input):
    """배출량 = 발전량(MWh) x 배출계수.

    손계산: 50 MW x 2h x 0.9 tCO2/MWh = 90 tCO2.
    """
    coal = make_generator(gen_id="coal", p_min_mw=0.0, co2_ton_per_mwh=0.9)
    result = baseline.run(make_input([coal], [50.0, 50.0], reserve_margin_ratio=0.0))

    assert carbon.hourly_ton(result.units) == pytest.approx([45.0, 45.0])
    assert carbon.total_ton(result.units) == pytest.approx(90.0)


def test_by_fuel_and_by_unit(make_generator, make_input):
    """발전원별·유닛별 집계가 총량과 일치한다."""
    coal = make_generator(gen_id="coal_1", fuel="bituminous_coal", p_min_mw=0.0,
                          co2_ton_per_mwh=0.9, fuel_cost_krw_per_mwh=90_000)
    nuclear = make_generator(gen_id="nuc_1", fuel="nuclear", p_min_mw=0.0,
                             co2_ton_per_mwh=0.006, fuel_cost_krw_per_mwh=6_500)
    result = baseline.run(make_input([coal, nuclear], [150.0], reserve_margin_ratio=0.0))

    by_fuel = carbon.by_fuel_ton(result.units)
    by_unit = carbon.by_unit_ton(result.units)
    assert by_fuel["nuclear"] == pytest.approx(100 * 0.006)   # 싼 원자력이 먼저 100 MW
    assert by_fuel["bituminous_coal"] == pytest.approx(50 * 0.9)
    assert sum(by_fuel.values()) == pytest.approx(carbon.total_ton(result.units))
    assert sum(by_unit.values()) == pytest.approx(carbon.total_ton(result.units))


def test_intensity_is_weighted_average(make_generator, make_input):
    coal = make_generator(gen_id="coal_1", p_min_mw=0.0, co2_ton_per_mwh=1.0,
                          fuel_cost_krw_per_mwh=90_000)
    clean = make_generator(gen_id="clean_1", p_min_mw=0.0, co2_ton_per_mwh=0.0,
                           fuel_cost_krw_per_mwh=0.0)
    result = baseline.run(make_input([coal, clean], [150.0], reserve_margin_ratio=0.0))

    # clean 100 MWh (0 t) + coal 50 MWh (50 t) -> 50 / 150
    assert carbon.intensity_ton_per_mwh(result.units) == pytest.approx(50 / 150)


def test_step_h_scales_energy(make_generator, make_input):
    """time step 이 1시간이 아니면 배출량도 그만큼 달라진다."""
    coal = make_generator(gen_id="coal", p_min_mw=0.0, co2_ton_per_mwh=1.0)
    result = baseline.run(make_input([coal], [100.0], reserve_margin_ratio=0.0))

    assert carbon.total_ton(result.units, step_h=0.5) == pytest.approx(50.0)


def test_compare_two_schedules(make_generator, make_input):
    """ESS 전/후, baseline vs MILP 비교에 쓰는 형태."""
    coal = make_generator(gen_id="coal", p_min_mw=0.0, co2_ton_per_mwh=1.0)
    before = baseline.run(make_input([coal], [100.0], reserve_margin_ratio=0.0))
    after = baseline.run(make_input([coal], [80.0], reserve_margin_ratio=0.0))

    diff = carbon.compare(before, after)
    assert diff["before_ton"] == pytest.approx(100.0)
    assert diff["after_ton"] == pytest.approx(80.0)
    assert diff["delta_ton"] == pytest.approx(-20.0)
    assert diff["reduction_pct"] == pytest.approx(20.0)


def test_summary_shape(make_generator, make_input):
    coal = make_generator(gen_id="coal", p_min_mw=0.0, co2_ton_per_mwh=0.5)
    result = baseline.run(make_input([coal], [100.0] * 3, reserve_margin_ratio=0.0))

    summary = carbon.summary(result)
    assert set(summary) == {"total_ton", "hourly_ton", "by_fuel_ton", "intensity_ton_per_mwh"}
    assert len(summary["hourly_ton"]) == 3


def test_empty_schedule_is_zero():
    assert carbon.hourly_ton([]) == []
    assert carbon.total_ton([]) == 0
    assert carbon.intensity_ton_per_mwh([]) == 0
