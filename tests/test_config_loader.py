"""config_loader 단위 테스트.

중점은 두 가지다.
  1. 원/kWh -> 원/MWh 변환이 정확히 한 번 일어나는지
  2. 잘못된 config 가 조용히 통과하지 않는지 (메시지에 위치가 담기는지)
"""

from __future__ import annotations

import pytest
import yaml

from loaders.config_loader import (
    ConfigError,
    load_generator_specs,
    load_settings,
    resolve_fuel_costs,
    select_fuel_cost_month,
)


def valid_generator(**overrides) -> dict:
    """불변식을 모두 만족하는 유닛 하나. 테스트마다 필요한 필드만 덮어쓴다."""
    base = {
        "id": "lng_1",
        "fuel": "lng",
        "p_max_mw": 2000,
        "p_min_mw": 800,
        "ramp_up_mw_per_h": 2000,
        "ramp_down_mw_per_h": 2000,
        "min_up_h": 2,
        "min_down_h": 2,
        "startup_cost_krw": 40000000,
        "co2_ton_per_mwh": 0.458,
        "u_init": 1,
        "init_state_h": 4,
    }
    base.update(overrides)
    return base


def write_generators(tmp_path, *entries):
    path = tmp_path / "generators.yaml"
    path.write_text(yaml.safe_dump({"generators": list(entries)}), encoding="utf-8")
    return path


FUEL_COST_ROWS = [
    {"month": "2026-08", "fuel": "lng", "cost_won_per_kwh": 155.09},
    {"month": "2026-09", "fuel": "lng", "cost_won_per_kwh": 156.641},
    {"month": "2026-09", "fuel": "nuclear", "cost_won_per_kwh": 6.585},
]


# ── 단위 변환 ────────────────────────────────────────────────────────────────


def test_fuel_cost_converted_kwh_to_mwh(tmp_path):
    """원/kWh 에 1000 을 곱해 원/MWh 가 된다. 이 변환은 로더에서만 일어난다."""
    specs = load_generator_specs(write_generators(tmp_path, valid_generator()))
    gens = resolve_fuel_costs(specs, FUEL_COST_ROWS, "2026-09")

    assert gens[0].fuel_cost_krw_per_mwh == pytest.approx(156641.0)


def test_fuel_cost_override_skips_join(tmp_path):
    """yaml 에 fuel_cost_krw_per_mwh 가 있으면 조인하지 않는다 (수력 0)."""
    hydro = valid_generator(
        id="hydro_1", fuel="hydro", p_min_mw=0, fuel_cost_krw_per_mwh=0,
    )
    specs = load_generator_specs(write_generators(tmp_path, hydro))

    # fuel_cost 에 hydro 가 없어도 통과해야 한다.
    gens = resolve_fuel_costs(specs, FUEL_COST_ROWS, "2026-09")
    assert gens[0].fuel_cost_krw_per_mwh == 0.0


def test_missing_fuel_cost_raises_instead_of_defaulting_to_zero(tmp_path):
    """조인 실패를 0 으로 때우면 그 발전기가 공짜가 되어 merit order 가 뒤집힌다."""
    coal = valid_generator(id="coal_1", fuel="bituminous_coal")
    specs = load_generator_specs(write_generators(tmp_path, coal))

    with pytest.raises(ConfigError, match="bituminous_coal"):
        resolve_fuel_costs(specs, FUEL_COST_ROWS, "2026-09")


def test_month_selection_and_fallback():
    assert select_fuel_cost_month(FUEL_COST_ROWS, "latest") == "2026-09"
    assert select_fuel_cost_month(FUEL_COST_ROWS, "2026-08") == "2026-08"
    # 없는 월은 최신으로 대체한다 (경고 로그와 함께).
    assert select_fuel_cost_month(FUEL_COST_ROWS, "2025-01") == "2026-09"


def test_different_month_gives_different_cost(tmp_path):
    """연료비가 월별로 바뀌는 값임을 로더가 반영한다."""
    specs = load_generator_specs(write_generators(tmp_path, valid_generator()))

    august = resolve_fuel_costs(specs, FUEL_COST_ROWS, "2026-08")[0]
    september = resolve_fuel_costs(specs, FUEL_COST_ROWS, "2026-09")[0]

    assert august.fuel_cost_krw_per_mwh == pytest.approx(155090.0)
    assert september.fuel_cost_krw_per_mwh == pytest.approx(156641.0)


# ── 불변식 ───────────────────────────────────────────────────────────────────


def test_p_min_above_p_max_rejected(tmp_path):
    path = write_generators(tmp_path, valid_generator(p_min_mw=3000, p_max_mw=2000))

    with pytest.raises(ConfigError) as exc:
        load_generator_specs(path)
    # 메시지에 유닛 id 가 있어야 어느 유닛인지 바로 안다.
    assert "lng_1" in str(exc.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"p_max_mw": 0},
        {"p_min_mw": -1},
        {"ramp_up_mw_per_h": 0},
        {"ramp_down_mw_per_h": -5},
        {"min_up_h": 0},
        {"min_down_h": 0},
        {"startup_cost_krw": -1},
        {"co2_ton_per_mwh": -0.1},
        {"u_init": 2},
        {"init_state_h": -1},
    ],
)
def test_invalid_values_rejected(tmp_path, overrides):
    with pytest.raises(ConfigError):
        load_generator_specs(write_generators(tmp_path, valid_generator(**overrides)))


def test_missing_required_field_rejected(tmp_path):
    entry = valid_generator()
    del entry["min_down_h"]

    with pytest.raises(ConfigError, match="min_down_h"):
        load_generator_specs(write_generators(tmp_path, entry))


def test_unknown_fuel_code_rejected(tmp_path):
    """fuel 은 collector domain.py FUELS 9종이어야 fuel_cost 조인이 된다."""
    with pytest.raises(ConfigError, match="coal"):
        load_generator_specs(write_generators(tmp_path, valid_generator(fuel="coal")))


def test_duplicate_id_rejected(tmp_path):
    path = write_generators(tmp_path, valid_generator(), valid_generator())

    with pytest.raises(ConfigError, match="중복"):
        load_generator_specs(path)


def test_non_numeric_value_rejected(tmp_path):
    with pytest.raises(ConfigError, match="p_max_mw"):
        load_generator_specs(write_generators(tmp_path, valid_generator(p_max_mw="2000")))


def test_empty_generators_rejected(tmp_path):
    path = tmp_path / "generators.yaml"
    path.write_text("generators: []", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_generator_specs(path)


def test_missing_file_rejected(tmp_path):
    with pytest.raises(ConfigError, match="없다"):
        load_generator_specs(tmp_path / "없는파일.yaml")


# ── settings ─────────────────────────────────────────────────────────────────


def write_settings(tmp_path, **overrides):
    data = {
        "horizon_h": 24,
        "time_step_min": 60,
        "timezone": "Asia/Seoul",
        "solver": "HiGHS",
        "reserve_margin_ratio": 0.10,
        "resample": "mean",
        "net_demand_mode": "subtract_solar_renewable",
    }
    data.update(overrides)
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_settings_defaults(tmp_path):
    settings = load_settings(write_settings(tmp_path))

    assert settings.horizon_h == 24
    assert settings.resample == "mean"
    assert settings.reserve_margin_ratio == pytest.approx(0.10)
    assert settings.fuel_cost_month == "latest"


def test_invalid_resample_rejected(tmp_path):
    with pytest.raises(ConfigError, match="resample"):
        load_settings(write_settings(tmp_path, resample="median"))


def test_invalid_net_demand_mode_rejected(tmp_path):
    with pytest.raises(ConfigError, match="net_demand_mode"):
        load_settings(write_settings(tmp_path, net_demand_mode="guess"))


def test_non_hourly_time_step_rejected(tmp_path):
    """로더와 MILP 가 1시간 step 만 지원한다. 조용히 통과하면 단위가 어긋난다."""
    with pytest.raises(ConfigError, match="time_step_min"):
        load_settings(write_settings(tmp_path, time_step_min=30))


# ── 실제 레포 config ─────────────────────────────────────────────────────────


def test_repo_config_loads(repo_root):
    """레포에 커밋된 config 가 실제로 로딩되는지 (팀원이 값을 바꿔도 깨지면 바로 잡힌다)."""
    settings = load_settings(repo_root / "config" / "settings.yaml")
    specs = load_generator_specs(repo_root / "config" / "generators.yaml")

    assert settings.time_step_min == 60
    assert 20 <= len(specs) <= 40, "대표유닛 권장 범위 (VPP_PROJECT_CONTEXT 9.4)"
    assert all(s.p_min_mw <= s.p_max_mw for s in specs)
