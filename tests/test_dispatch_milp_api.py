"""POST /api/v1/dispatch/milp 계약 테스트.

중점
  1. 응답 스키마와 HTTP status
  2. 요청 옵션(수요 직접 주기, 유닛 선택, 슬랙, baseline 포함)이 동작하는지
  3. infeasible 은 200 + status 로, 잘못된 입력은 4xx 로 나가는지
  4. 10초 KPI 를 응답에서 확인할 수 있는지
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import forecast_client
import main
import service
from shared.schemas import MilpResponse, ScenarioProfile


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


# 수요를 직접 주는 요청. 수집 데이터에 의존하지 않아 빠르고 결정적이다.
FLAT_DEMAND = [65000.0] * 24  # 수집 데이터 1시간 평균 수요 65,152 MW 기준


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_milp_with_explicit_demand(client):
    body = {"horizon_h": 24, "net_demand_mw": FLAT_DEMAND, "include_units": False}
    response = client.post("/api/v1/dispatch/milp", json=body)

    assert response.status_code == 200
    payload = MilpResponse.model_validate(response.json())
    assert payload.status == "optimal"
    assert payload.horizon_h == 24
    assert len(payload.timestamps) == 24
    assert len(payload.hours) == 24
    assert payload.units is None
    assert payload.cost.total_won == pytest.approx(
        payload.cost.fuel_won + payload.cost.startup_won + payload.cost.shed_won
    )


def test_response_meets_runtime_kpi(client):
    """MILP API 응답시간 <= 10초 (CLAUDE.md 정량 기준)."""
    body = {"net_demand_mw": FLAT_DEMAND, "include_units": False}
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp", json=body).json()
    )

    assert payload.model_size.total_time_s < 10.0
    assert payload.model_size.n_binaries > 0


def test_all_constraints_pass_on_normal_case(client):
    """정상 해에서는 필수 제약 위반이 0 이어야 한다."""
    body = {"net_demand_mw": FLAT_DEMAND, "include_units": False}
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp", json=body).json()
    )

    failed = [c.name for c in payload.constraints if not c.ok]
    assert failed == []
    assert all(h.reserve_pct >= 10.0 - 1e-6 for h in payload.hours)


def test_units_are_returned_when_requested(client):
    body = {"horizon_h": 3, "net_demand_mw": [65000.0] * 3, "include_units": True}
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp", json=body).json()
    )

    assert payload.units is not None
    unit_ids = {u.unit_id for u in payload.units}
    assert len(payload.units) == len(unit_ids) * 3


def test_baseline_comparison_is_included(client):
    body = {"net_demand_mw": FLAT_DEMAND, "include_units": False, "include_baseline": True}
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp", json=body).json()
    )

    assert payload.baseline is not None
    assert payload.baseline.method == "rule_based"
    assert payload.baseline.rules  # 규칙이 응답에 문서화돼 있다
    assert payload.baseline.total_cost_won >= payload.cost.total_won
    assert payload.baseline.cost_reduction_pct >= 0


def test_baseline_can_be_skipped(client):
    body = {"net_demand_mw": FLAT_DEMAND, "include_units": False, "include_baseline": False}
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp", json=body).json()
    )

    assert payload.baseline is None


def test_unit_subset_is_respected(client):
    # 원자력 4기만으로 풀 수 있는 수요를 준다 (새울 3호기는 계획예방정비로 제외.
    # 설비 20,840 MW, 최소출력 합 16,672 MW, 예비율 10% 포함 18,000 x 1.1 = 19,800 MW).
    body = {
        "horizon_h": 2,
        "net_demand_mw": [18000.0, 18000.0],
        "unit_ids": ["nuclear_1", "nuclear_2", "nuclear_3", "nuclear_4"],
    }
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp", json=body).json()
    )

    assert payload.status == "optimal"
    assert {u.unit_id for u in payload.units} == set(body["unit_ids"])


def test_unknown_unit_id_is_rejected(client):
    body = {"horizon_h": 1, "net_demand_mw": [1000.0], "unit_ids": ["does_not_exist"]}
    response = client.post("/api/v1/dispatch/milp", json=body)

    assert response.status_code == 500
    assert "does_not_exist" in response.json()["detail"]


def test_demand_length_mismatch_is_422(client):
    body = {"horizon_h": 24, "net_demand_mw": [65000.0, 65000.0]}
    response = client.post("/api/v1/dispatch/milp", json=body)

    assert response.status_code == 422
    assert "net_demand_mw" in response.json()["detail"]


def test_infeasible_returns_200_with_status(client):
    """용량을 넘는 수요는 200 + status=infeasible + 진단 notes 로 돌려준다."""
    body = {"horizon_h": 1, "net_demand_mw": [500000.0], "include_units": False}
    response = client.post("/api/v1/dispatch/milp", json=body)

    assert response.status_code == 200
    payload = MilpResponse.model_validate(response.json())
    assert payload.status == "infeasible"
    assert payload.cost.total_won == 0
    assert any("solver status" in note for note in payload.notes)


def test_load_shedding_option_quantifies_shortage(client):
    """슬랙을 켜면 부족량이 shed_mw 로 나온다 (옵션이며 기본은 off)."""
    body = {
        "horizon_h": 1,
        "net_demand_mw": [500000.0],
        "allow_load_shedding": True,
        "include_units": False,
        "include_baseline": False,
    }
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp", json=body).json()
    )

    assert payload.status == "optimal"
    assert payload.hours[0].shed_mw > 0
    assert payload.cost.shed_won > 0
    assert any("부하차단" in note for note in payload.notes)


def test_reserve_ratio_override(client):
    """요청으로 예비율을 바꿀 수 있다 (위기 시나리오 판정에 쓴다)."""
    body = {"horizon_h": 1, "net_demand_mw": [65000.0], "reserve_ratio": 0.3,
            "include_units": False, "include_baseline": False}
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp", json=body).json()
    )

    assert payload.reserve_ratio == 0.3
    assert payload.hours[0].reserve_pct >= 30.0 - 1e-6


def test_hydro_limit_shows_in_response(client):
    """저수율을 낮게 주면 hydro_factor 와 notes 에 드러난다."""
    body = {"horizon_h": 1, "net_demand_mw": [65000.0], "storage_rate_pct": 15.0,
            "include_units": False, "include_baseline": False}
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp", json=body).json()
    )

    assert payload.hydro_factor == pytest.approx(0.75)
    assert any("저수율" in note for note in payload.notes)


def test_carbon_and_merit_order_are_consistent(client):
    body = {"net_demand_mw": FLAT_DEMAND, "include_units": False, "include_baseline": False}
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp", json=body).json()
    )

    assert payload.carbon.total_ton == pytest.approx(sum(payload.carbon.hourly_ton))
    assert payload.carbon.total_ton == pytest.approx(
        sum(payload.carbon.by_fuel_ton.values())
    )
    assert payload.carbon.total_ton == pytest.approx(
        sum(m.co2_ton for m in payload.merit_order)
    )
    ranks = [m.fuel_cost_krw_per_mwh for m in payload.merit_order]
    assert ranks == sorted(ranks)


def test_demand_from_collected_data(client):
    """demand_source=data 면 수집 데이터의 최신 연속 구간을 쓴다."""
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp",
                    json={"horizon_h": 6, "demand_source": "data",
                          "include_units": False}).json()
    )

    assert payload.status == "optimal"
    assert payload.demand_source == "data"
    assert len(payload.timestamps) == 6
    gaps = {
        (b - a).total_seconds()
        for a, b in zip(payload.timestamps, payload.timestamps[1:], strict=False)
    }
    assert gaps == {3600.0}


# ── ESS ──────────────────────────────────────────────────────────────────────


def test_ess_is_included_by_default(client):
    """use_ess 기본값은 true 다. 응답에 ESS 운전 계획과 피크 지표가 담긴다."""
    body = {"net_demand_mw": FLAT_DEMAND, "include_units": False, "include_baseline": False}
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp", json=body).json()
    )

    ess = payload.ess
    assert ess is not None
    assert len(ess.charge_mw) == len(ess.discharge_mw) == len(ess.soc_mwh) == 24
    assert ess.peak_before_mw > 0
    assert ess.peak_target_pct == 5.0
    # 시간 요약의 ess_mw 는 방전(+) - 충전(-)
    for t, hour in enumerate(payload.hours):
        assert hour.ess_mw == pytest.approx(ess.discharge_mw[t] - ess.charge_mw[t])


def test_ess_can_be_disabled(client):
    """use_ess: false 면 ESS 없이 풀고, 전/후 비교의 '전' 쪽이 된다."""
    body = {"net_demand_mw": FLAT_DEMAND, "include_units": False,
            "include_baseline": False, "use_ess": False}
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp", json=body).json()
    )

    assert payload.ess is None
    assert all(h.ess_mw == 0 for h in payload.hours)
    assert not any(c.name.startswith("ESS") for c in payload.constraints)


def test_peak_shaving_mode_meets_target(client):
    """peak_shaving 모드는 피크 감소율 5% 를 하드 제약으로 보장한다."""
    demand = [65000.0] * 12 + [78000.0, 82000.0, 84000.0, 83000.0] + [65000.0] * 8
    body = {"net_demand_mw": demand, "include_units": False, "include_baseline": False,
            "ess_mode": "peak_shaving", "ess_peak_target_pct": 5.0}
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp", json=body).json()
    )

    assert payload.status == "optimal"
    ess = payload.ess
    assert ess.mode == "peak_shaving"
    assert ess.peak_reduction_pct >= 5.0
    assert ess.target_met is True
    assert ess.peak_after_mw <= ess.peak_before_mw * 0.95 + 1e-6
    assert [c.name for c in payload.constraints if not c.ok] == []


def test_impossible_peak_target_is_infeasible(client):
    """달성 불가한 목표는 infeasible 로 드러나고 제약 검증은 생략된다."""
    demand = [65000.0] * 23 + [90000.0]
    body = {"net_demand_mw": demand, "include_units": False, "include_baseline": False,
            "ess_mode": "peak_shaving", "ess_peak_target_pct": 30.0}
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp", json=body).json()
    )

    assert payload.status == "infeasible"
    assert [c.name for c in payload.constraints] == ["제약 검증"]


def test_ess_energy_balance_in_response(client):
    """응답의 충전·방전·손실이 왕복효율과 일치한다."""
    demand = [65000.0] * 12 + [80000.0] * 4 + [65000.0] * 8
    body = {"net_demand_mw": demand, "include_units": False, "include_baseline": False,
            "ess_mode": "peak_shaving"}
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp", json=body).json()
    )

    ess = payload.ess
    soc_gain = ess.soc_mwh[-1] - ess.soc_init_mwh
    assert ess.loss_mwh == pytest.approx(
        ess.energy_charged_mwh - ess.energy_discharged_mwh - soc_gain, abs=1.0
    )
    assert ess.loss_mwh >= 0


def test_baseline_comparison_notes_ess_condition(client):
    """ESS 를 쓰면 절감률 비교 조건을 notes 에 밝힌다 (baseline 은 ESS 없음)."""
    body = {"net_demand_mw": FLAT_DEMAND, "include_units": False, "include_baseline": True}
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp", json=body).json()
    )

    assert payload.baseline is not None
    assert any("ESS" in note and "미정" in note for note in payload.notes)


# ── forecast_api 수요 (demand_source=forecast, 기본) ─────────────────────────


def _forecast_profile() -> ScenarioProfile:
    """forecast_client.latest_base 가 돌려주는 모양. 2026-11-09 0시부터 24시간."""
    start = datetime(2026, 11, 9, tzinfo=forecast_client.KST)
    return ScenarioProfile(
        demand_mw=[60000.0] * 24, solar_mw=[3000.0] * 24, wind_mw=[500.0] * 24,
        start=start, issued_at=start - timedelta(hours=13), source="forecast_api",
    )


def test_demand_from_forecast_by_default(client, monkeypatch):
    """수요를 안 주면 forecast_api 예측의 순수요(수요 - 태양광 - 풍력 - 기저 신재생)를 쓴다."""
    monkeypatch.setattr(service, "cached_latest_base", lambda: _forecast_profile())
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp", json={"include_units": False}).json()
    )

    assert payload.status == "optimal"
    assert payload.demand_source == "forecast"
    assert payload.timestamps[0] == datetime(2026, 11, 9, tzinfo=forecast_client.KST)
    renewable = 60000.0 - 3000.0 - 500.0 - payload.hours[0].demand_mw  # 최근 실측 평균
    assert all(h.demand_mw == pytest.approx(payload.hours[0].demand_mw) for h in payload.hours)
    assert 0.0 < renewable < 5000.0
    assert any("forecast_api" in note for note in payload.notes)


def test_forecast_unavailable_falls_back_to_data(client, monkeypatch):
    """예측을 받을 수 없으면 수집 실측으로 풀고 notes 에 남긴다."""
    def unavailable():
        raise forecast_client.ForecastUnavailableError("forecast_api 호출 실패: 테스트")

    monkeypatch.setattr(service, "cached_latest_base", unavailable)
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp", json={"include_units": False}).json()
    )

    assert payload.status == "optimal"
    assert payload.demand_source == "data"
    assert any("수집 실측" in note for note in payload.notes)


def test_forecast_rejects_other_horizon(client, monkeypatch):
    """예측은 24시간이라 다른 horizon_h 는 422 로 거절한다."""
    monkeypatch.setattr(service, "cached_latest_base", lambda: _forecast_profile())
    response = client.post("/api/v1/dispatch/milp", json={"horizon_h": 6})
    assert response.status_code == 422
