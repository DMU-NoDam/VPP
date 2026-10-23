"""POST /api/v1/dispatch/milp 계약 테스트.

중점
  1. 응답 스키마와 HTTP status
  2. 요청 옵션(수요 직접 주기, 유닛 선택, 슬랙, baseline 포함)이 동작하는지
  3. infeasible 은 200 + status 로, 잘못된 입력은 4xx 로 나가는지
  4. 10초 KPI 를 응답에서 확인할 수 있는지
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main
from shared.schemas import MilpResponse


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


# 수요를 직접 주는 요청. 수집 데이터에 의존하지 않아 빠르고 결정적이다.
FLAT_DEMAND = [60000.0] * 24


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
    body = {"horizon_h": 3, "net_demand_mw": [60000.0] * 3, "include_units": True}
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
    # 원자력 4기만으로 풀 수 있는 수요를 준다 (설비 26,050 MW, 예비율 10% 포함).
    body = {
        "horizon_h": 2,
        "net_demand_mw": [20000.0, 20000.0],
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
    body = {"horizon_h": 24, "net_demand_mw": [60000.0, 60000.0]}
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
    body = {"horizon_h": 1, "net_demand_mw": [60000.0], "reserve_ratio": 0.3,
            "include_units": False, "include_baseline": False}
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp", json=body).json()
    )

    assert payload.reserve_ratio == 0.3
    assert payload.hours[0].reserve_pct >= 30.0 - 1e-6


def test_hydro_limit_shows_in_response(client):
    """저수율을 낮게 주면 hydro_factor 와 notes 에 드러난다."""
    body = {"horizon_h": 1, "net_demand_mw": [60000.0], "storage_rate_pct": 15.0,
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
    """수요를 안 주면 수집 데이터의 최신 연속 구간을 쓴다."""
    payload = MilpResponse.model_validate(
        client.post("/api/v1/dispatch/milp",
                    json={"horizon_h": 6, "include_units": False}).json()
    )

    assert payload.status == "optimal"
    assert len(payload.timestamps) == 6
    gaps = {
        (b - a).total_seconds()
        for a, b in zip(payload.timestamps, payload.timestamps[1:], strict=False)
    }
    assert gaps == {3600.0}
