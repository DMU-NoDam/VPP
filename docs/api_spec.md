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

## POST /api/v1/dispatch/stochastic (dispatch_api `:8002`)

Two-Stage Stochastic 급전 계획과 VSS. 스키마: `shared/schemas.py` 의 `StochasticRequest` / `StochasticResponse`.
정식화는 [formulation.md](formulation.md) 8절, VSS·EVPI 정의는 9절.

한 번 호출하면 RP(전 시나리오) → EV(평균 시나리오 1개) → EEV(EV 의 1단계 해 고정) 순으로 풀고,
`compute_ws` 가 켜져 있으면 WS(시나리오마다 1개씩) 를 더 푼다. `solver` 의 시간 제한·gap 은 **문제마다**
적용되므로 응답 시간은 최대 `time_limit_s` 의 3배 (WS 를 켜면 3 + `n_scenarios` 배) 다.

> **VSS 를 판정할 때는 `solver` 를 `{ "time_limit_s": 60, "mip_gap": 0.001 }` 로 준다.**
> 기본값(10초, 0.5%)에서는 25기 x 시나리오 10개 RP 가 시간 제한에 걸려 해가 나빠지고, VSS 가 음수로 나올 수 있다
> (측정 예: `status: time_limit`, `vss_pct` −36.8%). 대시보드는 이 조건으로 미리 계산해 둔
> `GET /dispatch/stochastic/latest` 를 쓴다.

요청 — `start` 만 필수다.

| 필드 | 기본값 | 설명 |
|---|---|---|
| `start` | (필수) | 계획 시작 시각. 응답 timestamp 의 기준 |
| `horizon_h` | 24 | 시나리오를 직접 주면 무시되고 시나리오 배열 길이를 쓴다 |
| `scenarios` | `null` | 시나리오 직접 입력. `null` 이면 `/scenarios` 와 같은 생성기로 `n_scenarios` 개를 만든다 (7절 파라메트릭 오차 모델) |
| `n_scenarios` | 10 | 생성 개수 (`scenarios` 가 `null` 일 때만) |
| `seed` | `null` | 생성 seed. `null` 이면 42 |
| `base` | `null` | 생성용 기준 예측 (`ScenarioProfile`). `null` 이면 생성기 기본 예측 (forecast_api 연동 전) |
| `compute_ws` | `false` | `true` 면 WS·EVPI 도 계산. 시나리오 수만큼 더 풀어 느려진다 |
| `generator_ids` | `null` | 사용할 유닛 id. `null` 이면 `generators.yaml` 전체 |
| `use_ess` | `true` | **현재 무시된다.** 2단계에 ESS 를 넣지 않았다 (8절 끝, 🟨 C 와 상의) |
| `reserve_ratio` | 0.10 | 1단계 예비율. 부족분은 슬랙으로 허용하고 `config/stochastic.yaml` 의 페널티를 붙인다 (가정, 8.1 S5 미확정) |
| `shed_penalty_won_per_mwh` | 10,000,000 | 부하차단 페널티 $K$. 🟨 VOLL 근거 미확정 |
| `include_scenario_hours` | `false` | `true` 면 `scenarios[].hours` 에 시나리오별 시간 요약을 넣는다 |
| `solver` | `{10, 0.005}` | 문제당 시간 제한(초)·상대 gap. 위 경고 참고 |

```json
{
  "start": "2026-09-22T00:00:00+09:00",
  "horizon_h": 24,
  "scenarios": null,
  "n_scenarios": 10,
  "seed": 1,
  "base": null,
  "compute_ws": false,
  "generator_ids": null,
  "reserve_ratio": 0.10,
  "shed_penalty_won_per_mwh": 10000000,
  "include_scenario_hours": false,
  "solver": { "time_limit_s": 60, "mip_gap": 0.001 }
}
```

시나리오를 직접 줄 때 (배열 길이 = `horizon_h`):

```json
"scenarios": [
  { "scenario_id": "S01", "probability": 0.1,
    "demand_mw": [61200.0, 59800.0], "solar_mw": [0.0, 0.0], "wind_mw": [1500.0, 1420.0] }
]
```

확률은 합이 1 이 아니어도 내부에서 정규화한다. 시나리오끼리 배열 길이가 다르면 422.

응답 필드:

| 필드 | 설명 |
|---|---|
| `status` | RP·EV·EEV 가 모두 `optimal` 이면 `optimal`, 아니면 RP → EV → EEV 순으로 처음 `optimal` 이 아닌 상태 (`feasible` · `time_limit` 등). `optimal` 이 아니면 VSS 를 믿지 않는다 |
| `solve_time_s` | RP·EV·EEV(·WS) 전체 풀이 시간 |
| `first_stage` | 유닛 x 시간 (25기 x 24h = 600행). `on` 은 1단계 기동계획 $u$. 1단계에는 출력 변수가 없어서 `p_mw` 는 시나리오 기대 출력 $\sum_s p_s P_{i,t,s}$ 다 |
| `expected_cost` | 시나리오 비용의 확률 가중 평균 (= RP 기대비용). `startup_won` = 1단계 기동비 + 당일 추가 기동비, `recourse_won` = 예비력 부족 페널티 (가정 항, 모든 시나리오에 같다), `shed_won` = 부하차단 페널티 |
| `expected_hours` | 시간별 수급의 확률 가중 평균. `reserve_pct` 의 공급능력은 가동 유닛 상한 합 + 태양광 + 풍력. `ess_mw` 는 항상 0 |
| `scenarios[]` | 시나리오별 `cost`, `shed_mwh`, `rt_adjust_mwh` (기대 출력 대비 그 시나리오 출력의 편차 $\sum\lvert P_s - E[P]\rvert$), `hours` |
| `vss` | `rp_won`·`ev_won`·`eev_won` 은 각 문제의 목적값. `vss_won` = EEV − RP, `vss_pct` = `vss_won` / `eev_won` x 100 (분모는 가정, 9절), `target_met` = `vss_pct` ≥ 3. `ws_won`·`evpi_won` 은 `compute_ws` 일 때만 |

오류:

| 코드 | 경우 |
|---|---|
| 422 | `start` 누락, 시나리오 길이 불일치, 선택된 발전기 없음 (`generator_ids` 가 모두 없는 id), 입력값 오류 |
| 500 | RP 의 해를 찾지 못함 (infeasible 또는 시간 안에 해 없음) |

응답 (숫자는 형태 예시):

```json
{
  "status": "optimal",
  "solve_time_s": 7.8,
  "start": "2026-09-22T00:00:00+09:00",
  "horizon_h": 24,
  "n_scenarios": 10,
  "first_stage": [
    { "timestamp": "2026-09-22T00:00:00+09:00", "unit_id": "nuclear_1", "on": true, "p_mw": 8500.0 }
  ],
  "expected_cost": {
    "fuel_won": 1.52e10, "startup_won": 3.1e8, "recourse_won": 4.2e8,
    "shed_won": 0.0, "total_won": 1.593e10
  },
  "expected_hours": [
    { "timestamp": "2026-09-22T00:00:00+09:00", "demand_mw": 61200.0, "supply_mw": 61200.0,
      "reserve_pct": 14.2, "shed_mw": 0.0, "ess_mw": 0.0, "co2_t": 31500.0 }
  ],
  "scenarios": [
    { "scenario_id": "S01", "probability": 0.1,
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
`precompute` 블록이고, 현재 값은 formulation.md 8.1 측정표의 seed 1 행과 같다 (시나리오 10개, 예비율 10%,
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
