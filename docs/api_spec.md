# API 명세 (API Spec)

## collector data_api (`:8000`)

수집한 원본을 그대로 내보낸다. 시각은 `"2026-09-22 15:55:00"` (`fuel_cost` 만 `"2026-09"`),
값이 없는 칸은 `"None"` (아래 결측치 참고), 응답은 시간 오름차순.

| 라우트 | 설명 |
|---|---|
| `GET /health` | 상태 + 데이터셋별 행 수 |
| `GET /datasets` | 데이터셋 정의 (컬럼·키·간격·행 수) |
| `GET /data/{name}?start=&end=&limit=` | 행 목록. `start`/`end` 는 시간 컬럼 기준 이상/이하, `limit` 은 뒤에서 N건. 없는 이름이면 404 |

`{name}`: `power_demand` `generation_by_fuel` `smp` `weather` `dam_status` `fuel_cost`
(컬럼은 `/datasets` 참고)

```json
{ "dataset": "power_demand", "count": 1,
  "rows": [{ "ts": "2026-09-22 09:00:00", "demand_mw": 71612.0 }] }
```

### 결측치 

- 0
값이 0
- "None" 
존재 하지 않는 값, 의미상 존재할 수 없는 값 (ex 풍력발전 발전 비용)
- "null"
조회 결과가 없는 값

## TODO: POST /api/v1/forecast

## POST /api/v1/dispatch/milp (dispatch_api `:8002`)

UC 포함 경제급전 MILP. 스키마는 `shared/schemas.py` 의 `MilpRequest` / `MilpResponse`,
정식화는 [formulation.md](formulation.md) 4장. 모든 필드에 기본값이 있어 `{}` 만
보내도 된다.

### 요청

```json
{
  "horizon_h": 24,
  "net_demand_mw": null,
  "start": null,
  "reserve_ratio": null,
  "storage_rate_pct": null,
  "p_init_mw": null,
  "unit_ids": null,
  "allow_load_shedding": false,
  "shed_penalty_won_per_mwh": null,
  "include_baseline": true,
  "include_units": true,
  "solver": { "time_limit_s": 10, "mip_gap": 0.005 }
}
```

| 필드 | 뜻 |
|---|---|
| `horizon_h` | 1~168, 기본 24 |
| `net_demand_mw` | 순수요 배열(MW, 길이 = `horizon_h`). 수요 - (태양광+풍력+신재생). 비우면 수집 데이터의 **최신 연속 구간**을 쓴다 |
| `start` | `net_demand_mw` 의 첫 시각. 비우면 현재 시(時), tz-aware |
| `reserve_ratio` | 예비율 요건. 비우면 `config/settings.yaml` (0.10) |
| `storage_rate_pct` | 저수율(%). 비우면 `dam_status` 최신 관측 평균 |
| `p_init_mw` | `{"coal_1": 3000.0}` 형태의 직전 출력. 없으면 t=0 램프 제약을 생략하고 `notes` 에 남긴다 |
| `unit_ids` | 쓸 유닛 일부만 지정 (발전소 탈락 시나리오). 없는 id 면 500 |
| `allow_load_shedding` | `[모델링 선택]` 기본 false. true 면 부족량이 `shed_mw` 로 나온다 |
| `include_baseline` | Rule-based 비교와 절감률 포함 (기본 true) |
| `include_units` | 유닛 x 시간 급전값 포함 (29기 x 24h = 696행) |
| `solver` | 요청별 time limit / MIP gap |

forecast_api 연동 전까지는 `net_demand_mw` 를 직접 주는 경로를 쓴다. `/api/v1/scenarios`
의 시나리오 하나를 `demand - solar - wind` 로 합쳐 넣으면 시나리오별 재실행이 된다.

### 응답 (배열은 앞부분만, 숫자는 실제 실행값)

```json
{
  "status": "optimal",
  "method": "milp",
  "horizon_h": 24,
  "timestamps": ["2026-04-10T00:00:00+09:00", "2026-04-10T01:00:00+09:00"],
  "model_size": {
    "n_variables": 2808, "n_binaries": 696, "n_constraints": 4564,
    "build_time_s": 0.026, "solve_time_s": 0.243, "total_time_s": 0.269
  },
  "cost": {
    "fuel_won": 74279713808.4, "startup_won": 340000000.0,
    "recourse_won": 0.0, "shed_won": 0.0, "total_won": 74619713808.4
  },
  "hours": [
    { "timestamp": "2026-04-10T00:00:00+09:00", "demand_mw": 56425.6,
      "supply_mw": 56425.6, "reserve_pct": 21.4, "shed_mw": 0.0,
      "ess_mw": 0.0, "co2_t": 26823.7 }
  ],
  "units": [
    { "timestamp": "2026-04-10T00:00:00+09:00", "unit_id": "nuclear_1",
      "on": true, "p_mw": 6512.5 }
  ],
  "merit_order": [
    { "rank": 1, "fuel": "hydro", "fuel_cost_krw_per_mwh": 0.0,
      "capacity_mw": 1609.8, "energy_mwh": 38635.2, "unit_hours_on": 48.0,
      "n_units": 2.0, "co2_ton": 2955.6 }
  ],
  "carbon": {
    "total_ton": 714512.2, "hourly_ton": [26823.7, 24817.0],
    "by_fuel_ton": { "nuclear": 3594.9, "bituminous_coal": 702087.9 },
    "intensity_ton_per_mwh": 0.498
  },
  "constraints": [
    { "name": "수급 균형", "ok": true, "detail": "위반 없음" },
    { "name": "예비율", "ok": true, "detail": "위반 없음" }
  ],
  "baseline": {
    "method": "rule_based",
    "rules": ["R1 우선순위: 연료비 오름차순 (동률이면 큰 유닛 먼저)"],
    "total_cost_won": 75116797926.6, "fuel_won": 74756797926.6,
    "startup_won": 360000000.0, "carbon_total_ton": 710747.7,
    "peak_mw": 66348.9, "cost_reduction_pct": 0.66, "target_met": false,
    "constraints": [{ "name": "수급 균형", "ok": true, "detail": "위반 없음" }]
  },
  "reserve_ratio": 0.1,
  "hydro_factor": 1.0,
  "notes": ["유닛별 초기 출력(p_init_mw)이 없어 t=0 의 램프 제약을 생략했다"]
}
```

### 읽는 법

- `status`: `optimal` | `feasible` | `infeasible` | `time_limit` | `error`.
  **`optimal` 이 아니면 급전값은 유효하지 않다** (전부 0). `notes` 에 진단 순서가 담긴다.
- `constraints`: 해를 받은 뒤 제약을 다시 검사한 결과 9종
  (수급 균형 / 출력 상·하한 / 예비율 / 램프업 / 램프다운 / 최소 기동시간 /
  최소 정지시간 / 수력 저수율 제한 / 과잉발전 / 부하차단). 정상 해에서는 전부 `ok: true`.
- `model_size.total_time_s`: 응답시간 KPI(<= 10초)를 이 값으로 확인한다.
  (HTTP 왕복에는 데이터 로딩이 더해지지만, 로딩 결과는 파일 변경 전까지 캐시된다.)
- `baseline.cost_reduction_pct`: `(baseline - milp) / baseline x 100`. 기준 >= 10%.
  `target_met` 이 판정이다. 양쪽 모두 같은 회계(연료비 + 기동비)를 쓴다.
- `hours[].ess_mw`: ESS 미구현이라 현재 항상 0 이다 (자리만 잡아 둠).

### HTTP 상태

| 코드 | 경우 |
|---:|---|
| 200 | 최적화가 끝났다. infeasible 도 200 이며 `status` 로 구분한다 |
| 422 | 입력 시계열이 모자라거나 길이가 안 맞는다 (`net_demand_mw` 길이 != `horizon_h` 등) |
| 500 | config 오류 (없는 `unit_ids`, generators.yaml 규칙 위반) |
| 503 | solver 를 이 환경에서 쓸 수 없다 |


## POST /api/v1/scenarios (dispatch_api `:8002`)

불확실성 시나리오 생성기. 스키마: `shared/schemas.py` 의 `ScenarioRequest` / `ScenarioSetResponse`.
방법은 [formulation.md](formulation.md) 7절. 모든 필드에 기본값이 있어 `{}` 만 보내도 된다.

요청:

```json
{ "n_scenarios": 10, "n_samples": 300, "seed": 42,
  "demand_sigma_pct": 2.5, "demand_ar": 0.9, "solar_cloud_pct": 10.0, "wind_sigma_pct": 25.0,
  "base": null }
```

`base` 를 비우면 기본 하루 예측을 쓴다 (forecast_api 연동 전). `n_scenarios` 1~50, `n_samples` 50~2000, 범위 밖이면 422.

응답 (배열은 24개 중 앞부분만):

```json
{
  "horizon_h": 24, "n_samples": 300, "seed": 42,
  "base": { "demand_mw": [52000, 50000], "solar_mw": [0, 0], "wind_mw": [800.0, 982.2] },
  "scenarios": [
    { "scenario_id": "S01", "probability": 0.1, "label": "고수요·맑음·강풍",
      "peak_net_load_mw": 89150.2, "demand_dev_pct": 1.9, "solar_dev_pct": 6.3, "wind_dev_pct": 21.4,
      "demand_mw": [53110.4, 51290.8], "solar_mw": [0.0, 0.0], "wind_mw": [912.5, 1104.0] }
  ],
  "net_load_band": { "p5": [49870.1, 47950.3], "p50": [51150.6, 49020.0], "p95": [52430.9, 50120.7] },
  "demand_band":   { "p5": [50010.2, 48060.5], "p50": [51990.4, 49980.1], "p95": [54050.8, 51900.3] },
  "checks": [ { "name": "확률 합 = 1", "ok": true, "detail": "1.000000" } ]
}
```

`scenarios` 는 피크 순부하 내림차순이다 (S01 이 가장 빡빡한 날). 각 시나리오의 `scenario_id`·`probability`·`demand_mw`·`solar_mw`·`wind_mw` 를 그대로 `/dispatch/stochastic` 의 `scenarios` 로 넘기면 된다.

## POST /api/v1/dispatch/stochastic (초안)

스키마: `shared/schemas.py` 의 `StochasticRequest` / `StochasticResponse`.
정식화와 VSS 정의는 [formulation.md](formulation.md) 8~9절.

요청 — `scenarios` 를 비우면 dispatch_api 가 예측값+잔차로 `n_scenarios` 개를 만든다.

```json
{
  "start": "2026-09-22T00:00:00+09:00",
  "horizon_h": 24,
  "scenarios": null,
  "n_scenarios": 10,
  "seed": 42,
  "generator_ids": null,
  "use_ess": true,
  "reserve_ratio": 0.10,
  "shed_penalty_won_per_mwh": 10000000,
  "include_scenario_hours": false,
  "solver": { "time_limit_s": 10, "mip_gap": 0.005 }
}
```

시나리오를 직접 줄 때 (배열 길이 = `horizon_h`):

```json
"scenarios": [
  { "scenario_id": "s01", "probability": 0.1,
    "demand_mw": [61200.0, 59800.0], "solar_mw": [0.0, 0.0], "wind_mw": [1500.0, 1420.0] }
]
```

응답 (숫자는 형태 예시):

```json
{
  "status": "optimal",
  "solve_time_s": 7.8,
  "start": "2026-09-22T00:00:00+09:00",
  "horizon_h": 24,
  "n_scenarios": 10,
  "first_stage": [
    { "timestamp": "2026-09-22T00:00:00+09:00", "unit_id": "coal_1", "on": true, "p_mw": 21000.0 }
  ],
  "expected_cost": {
    "fuel_won": 1.52e10, "startup_won": 3.1e8, "recourse_won": 4.2e8,
    "shed_won": 0.0, "total_won": 1.593e10
  },
  "expected_hours": [
    { "timestamp": "2026-09-22T00:00:00+09:00", "demand_mw": 61200.0, "supply_mw": 61200.0,
      "reserve_pct": 14.2, "shed_mw": 0.0, "ess_mw": -300.0, "co2_t": 31500.0 }
  ],
  "scenarios": [
    { "scenario_id": "s01", "probability": 0.1,
      "cost": { "fuel_won": 1.49e10, "startup_won": 3.1e8, "recourse_won": 2.0e8,
                "shed_won": 0.0, "total_won": 1.541e10 },
      "shed_mwh": 0.0, "rt_adjust_mwh": 1850.0, "hours": null }
  ],
  "vss": {
    "rp_won": 1.593e10, "ev_won": 1.548e10, "eev_won": 1.651e10,
    "vss_won": 5.8e8, "vss_pct": 3.5,
    "ws_won": null, "evpi_won": null, "target_met": true
  }
}
```
