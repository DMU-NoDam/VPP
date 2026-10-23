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

## TODO: POST /api/v1/dispatch/milp

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
정식화와 VSS 정의는 [formulation.md](formulation.md) 3장.
1단계 변수는 기동계획뿐이라 `first_stage[].p_mw` 는 시나리오 기대 출력이고, `rt_adjust_mwh` 는 그 기대 출력 대비
시나리오 출력의 편차 합이다. `recourse_won` 에는 예비력 부족 페널티(가정 항)만 들어간다.

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

## GET /api/v1/dispatch/stochastic/latest

대시보드 VSS 타일용. dispatch_api 가 켜질 때와 `refresh_hours` 마다 백그라운드로 Two-Stage 를 한 번 풀어 두고,
이 엔드포인트는 마지막 결과를 바로 돌려준다 (풀이 자체는 1분 안팎). 계산 조건은 `config/stochastic.yaml` 의
`precompute` 블록이고, 현재 값은 formulation.md 3장 측정표의 seed 1 행과 같다 (시나리오 10개, 예비율 10%,
문제당 60초, gap 0.1%). 환경변수 `PRECOMPUTE_ENABLED=false` 면 계산하지 않는다.

스키마: `shared/schemas.py` 의 `StochasticLatest`.

| 필드 | 설명 |
|---|---|
| `state` | `pending` 시작 전 · `running` 계산 중 · `done` · `error` · `disabled` |
| `computed_at` | 마지막으로 성공한 계산 시각 (KST) |
| `seed`, `n_scenarios`, `reserve_ratio`, `shed_penalty_won_per_mwh`, `time_limit_s`, `mip_gap` | 계산 조건 |
| `result` | `StochasticResponse`. 아직 없으면 `null`. 계산 중이거나 실패해도 이전 결과는 남는다 |
| `error` | 마지막 실패 메시지 |

```json
{
  "state": "done", "computed_at": "2026-10-23T15:40:26+09:00",
  "seed": 1, "n_scenarios": 10, "reserve_ratio": 0.1, "shed_penalty_won_per_mwh": 10000000.0,
  "time_limit_s": 60.0, "mip_gap": 0.001,
  "result": { "status": "optimal", "solve_time_s": 58.1, "vss": { "vss_pct": 0.002, "...": "..." }, "...": "..." },
  "error": null
}
```
