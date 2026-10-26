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

## POST /api/v1/forecast (forecast_api `:8001`)

수요·태양광·풍력 시계열 예측. 향후 24~168시간의 예측값과 90% 예측구간(conformal)을 반환한다.
학습 데이터는 collector CSV(`power_demand`, `trading_by_fuel`, `weather`)를 쓴다.

요청:

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `start_date` | string (YYYY-MM-DD) | O | 과거 데이터 시작일 |
| `end_date` | string (YYYY-MM-DD) | O | 과거 데이터 종료일. 예측은 이 시점(데이터가 끝나는 시점) 다음 시간부터 |
| `target` | `"demand"` \| `"solar"` \| `"wind"` \| `"all"` | O | `all`이면 세 개를 한 번에 반환 |
| `horizon_hours` | integer (24~168) | O | 예측 시간 범위 |
| `model` | `"auto"` \| `"lightgbm"` \| `"lstm"` | X (기본 `auto`) | `auto`는 lightgbm. `lstm`은 서빙 미구현(501) |
| `pi_method` | `"conformal"` \| `"quantile"` | X (기본 `conformal`) | 현재 conformal만 구현 |

```json
{ "start_date": "2025-07-01", "end_date": "2026-10-12", "target": "all", "horizon_hours": 24 }
```

응답 (200, `points`는 앞부분만):

```json
{
  "result_code": "00", "result_msg": "OK",
  "generated_at": "2026-10-15T10:00:00", "horizon_hours": 24,
  "forecasts": {
    "demand": {
      "model_used": "lightgbm", "pi_method": "conformal",
      "metrics": { "mape": 3.22, "nmae": 3.3, "nmae_mean": 3.3, "coverage": 86.2, "interval_width_mean": 8445.0, "n_origins": 13.0, "primary_value": 3.22 },
      "points": [
        { "datetime": "2026-10-12T01:00:00", "predicted": 51686.4,
          "lower_bound": 50436.5, "upper_bound": 52936.3 }
      ]
    },
    "solar": { "...": "demand와 동일 구조, primary_value = nMAE" },
    "wind":  { "...": "demand와 동일 구조, primary_value = nMAE" }
  }
}
```

- `metrics`는 학습·PI 계산에 쓰지 않은 시험 구간(데이터 마지막 2주)에서 API와 같은 방식으로 예측한 성적이다
  (`horizon_hours` ≤ 24면 24h 성적, 그보다 길면 168h 성적). `primary_value`는 스펙 판정 지표(수요=MAPE, 태양광/풍력=nMAE),
  `coverage`는 90% 예측구간 커버리지(%), `interval_width_mean`은 평균 구간 폭(MW), `n_origins`는 평가에 쓴 예측 시작 시점 수,
  `nmae_mean`은 참고용(실제값 평균 기준) nMAE.
- **nMAE 정의**: `nMAE = 평균 절대오차 ÷ 설비용량 × 100`. 과제 문서에 정의가 없어 재생에너지 예측에서 일반적인
  설비용량 기준을 쓴다 (대시보드 `dashboard/logic.py`와 동일). 설비용량은 `trading_by_fuel.capacity_mw`.
- 예측 방식:
  - `demand`: LightGBM recursive. 피처 = 과거 수요(1h/24h/168h 전, 24h 평균) + 전국 평균 기온·냉방도일(CDD, 24℃)·
    난방도일(HDD, 18℃) + 시각·요일·공휴일(대체공휴일 포함).
  - `solar`: LightGBM direct. 7개 지점 일사량(+이동평균) + 달력으로 이용률(발전량/설비용량)을 예측하고 최신 설비용량을 곱한다.
  - `wind`: LightGBM direct. 6개 지점 풍속(+이동평균) + 달력으로 발전량(MW)을 예측한다.
- 예측구간(90%, conformal): 시험 구간 직전 4주(calibration) 오차로 margin을 정한다 (`saved/{target}_pi.json`).
  수요는 "몇 시간 뒤"별 margin(먼 미래일수록 넓음), 태양광/풍력은 "몇 시"별 margin(태양광은 밤에 거의 0).
- 단위: `demand`·`solar`·`wind` 모두 MW (발전량은 `trading_by_fuel.trade_mwh` 1시간 거래량 = 평균 MW).
- 태양광/풍력 예측은 `trading_by_fuel`의 마지막 시각 다음 시간부터 시작한다 (수요보다 데이터가 짧을 수 있음).
- 미래 기상(기온·일사량·풍속)은 예보 API가 없어서 해당 시각의 관측값을 예보 대용으로 쓰고(완벽한 예보 가정),
  관측값도 없으면 과거 시간대별 평균을 쓴다. 실제 운영 시에는 예보 오차만큼 성능이 낮아질 수 있다.

에러:

| HTTP | result_code | result_msg | 상황 |
|---|---|---|---|
| 500 | `21` | `MODEL_LOAD_ERROR` | 모델 파일 없음 (`models/train_lgbm.py` 미실행) |
| 501 | `21` | `MODEL_NOT_IMPLEMENTED` | `model: "lstm"` 요청 (서빙 미구현) |
| 422 | - | - | 요청 검증 실패 (FastAPI 기본 형식) |

```json
{ "detail": { "result_code": "21", "result_msg": "MODEL_LOAD_ERROR",
              "detail": ".../models/saved/demand_lgbm.txt 없음 — models/train_lgbm.py 먼저 실행 필요" } }
```

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
