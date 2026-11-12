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
  "rows": [{ "ts": "2026-10-26 10:10:00", "demand_mw": 60622.3, "supply_mw": 92726.6 }] }
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
| `model` | `"auto"` \| `"lightgbm"` \| `"lstm"` | X (기본 `auto`) | `auto`는 대상별로 기준을 통과한 모델 중 더 정확한 모델 (현재 세 대상 모두 LSTM). LSTM을 쓸 수 없으면 LightGBM으로 대체 |
| `pi_method` | `"conformal"` \| `"quantile"` | X (기본 `conformal`) | 현재 conformal만 구현. `quantile`은 501 (`MODEL_NOT_IMPLEMENTED`) |

```json
{ "start_date": "2025-07-01", "end_date": "2026-11-08", "target": "all", "horizon_hours": 24 }
```

응답 (200, `points`는 앞부분만):

```json
{
  "result_code": "00", "result_msg": "OK",
  "generated_at": "2026-11-09T10:00:00", "horizon_hours": 24,
  "forecasts": {
    "demand": {
      "model_used": "lstm", "pi_method": "conformal",
      "metrics": { "mape": 3.42, "nmae": 3.28, "nmae_mean": 3.28, "coverage": 88.78, "interval_width_mean": 6492.49, "n_origins": 13.0, "primary_value": 3.42 },
      "points": [
        { "datetime": "2026-11-08T01:00:00", "predicted": 55723.7,
          "lower_bound": 54570.5, "upper_bound": 56876.9 },
        { "datetime": "2026-11-08T13:00:00", "predicted": 49646.8,
          "lower_bound": 43178.1, "upper_bound": 56115.5 }
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
- 서빙 모델 (`model: "auto"`): LightGBM과 LSTM을 같은 시험 구간·같은 입력 정보로 비교해서
  대상별로 기준(MAPE/nMAE·Coverage)을 통과한 모델 중 더 정확한 쪽을 쓴다 → 수요 LSTM, 태양광 LSTM, 풍력 LSTM
  (`services/forecast_api/models/saved/model_comparison.md`). `model_used`에 실제로 쓴 모델이 나온다.
- 예측 방식:
  - `demand`(LSTM): 최근 24시간 시퀀스(1시간 전 수요 + 기온·냉난방도일·일사량·달력)로 1시간씩 이어서 예측.
    LightGBM일 때는 recursive. 피처 = 과거 수요(1h/24h/168h 전, 24h 평균)
    + 태양광 7개 지점 평균 일사량(맑은 낮에 자가용 태양광만큼 계통 수요가 줄어드는 효과) + 전국 평균 기온·냉방도일(CDD, 24℃)·
    난방도일(HDD, 18℃) + 시각·요일·공휴일(대체공휴일 포함).
  - `solar`(LSTM): 최근 24시간 시퀀스(1시간 전 발전량 + 7개 지점 일사량·이동평균 + 달력)로 이어서 예측.
    LightGBM일 때는 direct (일사량 + 달력으로 이용률(발전량/설비용량)을 예측하고 최신 설비용량을 곱한다).
  - `wind`(LSTM): 최근 24시간 시퀀스(1시간 전 발전량 + 6개 지점 풍속·이동평균 + 달력)로 이어서 예측.
    LightGBM일 때는 direct (풍속 + 달력으로 각 시각의 발전량을 바로 예측).
- 예측구간(90%, conformal): 시험 구간 직전 4주(calibration) 오차로 margin을 정한다 (`saved/{target}_pi.json`).
  LSTM과 LightGBM recursive(수요)는 "몇 시간 뒤 × 몇 시" 가변 폭: 폭 = q × (몇 시간 뒤 크기) × (몇 시 배율).
  먼 미래일수록 넓고, 오차가 큰 낮(10~15시, 맑은/흐린 날 차이)은 넓게, 새벽은 좁게 잡는다
  (수요 배율 예: 3시 약 0.5, 12시 약 2.0). `pi_type: "horizon_hour"`, 파라미터 `horizon_scale`·`hour_scale`·`q`.
  LightGBM direct(태양광·풍력)는 "몇 시"별 margin(태양광은 밤에 거의 0). LSTM은 `saved/{target}_lstm_eval.json`.
- 단위: `demand`·`solar`·`wind` 모두 MW (발전량은 `trading_by_fuel.trade_mwh` 1시간 거래량 = 평균 MW).
- 태양광/풍력 예측은 `trading_by_fuel`의 마지막 시각 다음 시간부터 시작한다 (수요보다 데이터가 짧을 수 있음).
- 미래 기상(기온·일사량·풍속)은 예보 API가 없어서 해당 시각의 관측값을 예보 대용으로 쓰고(완벽한 예보 가정),
  관측값도 없으면 과거 시간대별 평균을 쓴다. 실제 운영 시에는 예보 오차만큼 성능이 낮아질 수 있다.

에러:

| HTTP | result_code | result_msg | 상황 |
|---|---|---|---|
| 500 | `21` | `MODEL_LOAD_ERROR` | 모델 파일이 없거나 읽을 수 없음 (`models/train_lgbm.py` / `train_lstm.py` 미실행) |
| 501 | `21` | `MODEL_NOT_IMPLEMENTED` | `model: "lstm"`을 직접 지정했는데 torch가 설치되지 않음 |
| 422 | - | - | 요청 검증 실패 (FastAPI 기본 형식) |

```json
{ "detail": { "result_code": "21", "result_msg": "MODEL_LOAD_ERROR",
              "detail": ".../models/saved/demand_lgbm.txt 없음 — models/train_lgbm.py 먼저 실행 필요" } }
```

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
  "use_ess": true,
  "ess_mode": null,
  "ess_peak_target_pct": null,
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
| `use_ess` | 기본 true. false 면 ESS 없이 푼다 (피크 전/후 비교의 "전") |
| `ess_mode` | `cost`(기본) / `peak_shaving` / `combined`. 비우면 `config/ess.yaml`. 피크 5% KPI 는 `peak_shaving` 으로 본다 |
| `ess_peak_target_pct` | `peak_shaving` 목표 감소율(%). 비우면 ess.yaml (5.0) |
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
  "ess": {
    "mode": "peak_shaving",
    "power_charge_mw": 4700.0, "power_discharge_mw": 4700.0,
    "capacity_mwh": 32900.0, "round_trip_efficiency": 0.75,
    "soc_min_mwh": 3290.0, "soc_max_mwh": 31255.0, "soc_init_mwh": 16450.0,
    "charge_mw": [2271.1, 0.0], "discharge_mw": [0.0, 4424.0],
    "soc_mwh": [31255.0, 26146.6], "soc_pct": [95.0, 79.5],
    "energy_charged_mwh": 24698.7, "energy_discharged_mwh": 18524.0,
    "loss_mwh": 6174.7, "cycles": 0.66,
    "peak_before_mw": 84000.0, "peak_after_mw": 79300.0,
    "peak_reduction_pct": 5.6, "peak_target_pct": 5.0, "target_met": true
  },
  "constraints": [
    { "name": "수급 균형", "ok": true, "detail": "위반 없음" },
    { "name": "예비율", "ok": true, "detail": "위반 없음" },
    { "name": "ESS SOC 전이식", "ok": true, "detail": "위반 없음" }
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
- `constraints`: 해를 받은 뒤 제약을 다시 검사한 결과
  (수급 균형 / 출력 상·하한 / 예비율 / 램프업 / 램프다운 / 최소 기동시간 /
  최소 정지시간 / 수력 저수율 제한 / 과잉발전 / 부하차단, ESS 를 쓰면 + ESS 출력
  한계 / SOC 범위 / SOC 전이식 / 종료 SOC / 동시 충방전). 정상 해에서는 전부
  `ok: true`. `status` 가 optimal 이 아니면 검증할 계획이 없으므로 `제약 검증`
  한 줄만 돌아온다.
- `model_size.total_time_s`: 응답시간 KPI(<= 10초)를 이 값으로 확인한다.
  (HTTP 왕복에는 데이터 로딩이 더해지지만, 로딩 결과는 파일 변경 전까지 캐시된다.)
- `baseline.cost_reduction_pct`: `(baseline - milp) / baseline x 100`. 기준 >= 10%.
  `target_met` 이 판정이다. 양쪽 모두 같은 회계(연료비 + 기동비)를 쓴다.
- `hours[].ess_mw`: ESS 순출력 (+ 방전 / - 충전). `use_ess: false` 면 0.
- `ess`: 사양 + 시간대별 충·방전·SOC + 피크 저감 효과. `peak_reduction_pct` 의
  정의는 `(ESS 미적용 순수요 피크 - 적용 후 발전기 부하 피크) / 전 x 100` 이고
  기준은 5% 다 ([formulation.md](formulation.md) 5.4). `target_met` 이 판정이다.
  `cost` 모드는 피크 감소를 **보장하지 않는다** (단가 차이가 없으면 ESS 를 쓸
  이유가 없다). 5% 를 보장해야 하면 `peak_shaving` 을 쓰고, 물리적으로 불가능한
  목표면 `status: infeasible` 로 돌아온다.
- ESS 를 켠 채 `include_baseline: true` 로 받은 `cost_reduction_pct` 는
  "MILP + ESS vs ESS 없는 Rule-based" 비교다. 조건이 다르다는 사실이 `notes` 에
  들어간다 (baseline 에 ESS 를 포함할지는 `[미정]`).

### HTTP 상태

| 코드 | 경우 |
|---:|---|
| 200 | 최적화가 끝났다. infeasible 도 200 이며 `status` 로 구분한다 |
| 422 | 입력 시계열이 모자라거나 길이가 안 맞는다 (`net_demand_mw` 길이 != `horizon_h` 등) |
| 500 | config 오류 (없는 `unit_ids`, generators.yaml 규칙 위반) |
| 503 | solver 를 이 환경에서 쓸 수 없다 |


## POST /api/v1/scenarios (dispatch_api `:8002`)

불확실성 시나리오 생성기. 스키마: `shared/schemas.py` 의 `ScenarioRequest` / `ScenarioSetResponse`.
방법은 [formulation.md](formulation.md) 7절. 모든 필드를 비울 수 있고(`{}` 만 보내도 된다), 비운 필드는
`config/stochastic.yaml` 의 `scenario_generator` 값으로 채운다. 아래 요청 예시의 숫자가 현재 config 값이다.

요청:

```json
{ "n_scenarios": 10, "n_samples": 300, "seed": 42,
  "sigma_scale": 1.0, "demand_ar": 0.9, "solar_ar": 0.7, "wind_ar": 0.85,
  "solar_capacity_mw": 12000.0, "wind_capacity_mw": 8000.0,
  "base": null }
```

| 필드 | 설명 |
|---|---|
| `sigma_scale` | σ 배율 (> 0). 1 이면 σ = forecast_api 90% 구간 반폭 / 1.645 그대로. 민감도 분석용 |
| `demand_ar`·`solar_ar`·`wind_ar` | 오차의 시간 상관 AR(1) 계수 (0 이상 1 미만, 범위 밖이면 422) |
| `base` | 기준 예측 (`ScenarioProfile`). 비우면 아래 "기준 예측" |

`n_scenarios` 1~50, `n_samples` 50~2000, 범위 밖이면 422.

**기준 예측 (forecast_api 연동).** `base` 를 비우면 dispatch_api 가 forecast_api `POST /forecast` 로
수요·태양광·풍력의 예측과 90% 구간을 받는다 (`config/stochastic.yaml` 의 `forecast` 블록).

- Day-ahead 조건: 계획일 D 의 0~23시를 전날 11시까지의 데이터로 예측한다 (`lead` 13h → 13~36시간 뒤).
  forecast_api 에 `end_date` = 결정 시각, `horizon_hours` = 36 으로 요청하고 뒤의 24시간을 쓴다.
- 세 target 의 데이터 끝 시각이 다르면 (2026-11-03 기준 수요 10/26, 태양광·풍력 9/27) 세 target 이 모두
  결정 시각까지 데이터가 있는 가장 최근 D 를 쓴다 (지금은 9/27 11시 데이터로 9/28).
- 구간 반폭은 `upper_bound − predicted` 다 (`lower_bound` 는 0 에서 잘려 있어 쓰지 않는다). 시나리오 오차
  σ = 반폭 / 1.645 x `sigma_scale` (formulation.md 7.3).
- 태양광·풍력의 음수 예측은 0 으로 둔다. forecast_api 의 `wind` 는 풍력 단독이다.
- forecast_api 를 쓸 수 없으면 `scenario_generator.base_profile` (구간 없음) 로 폴백하고, σ 는
  `fallback_sigma` (예측값 대비 %) 다.
- 🟨 `end_date` 에 시각(`"2026-09-27 11:00:00"`)을 넣는 것은 forecast_api 스키마 설명(YYYY-MM-DD)에 없는
  사용법이다. 동작은 확인했고 B 와 계약으로 맞춰야 한다.

응답의 `base` 에 쓴 기준 예측이 그대로 실린다: `source` (`forecast_api` / `config`), `start` (계획 첫 시각),
`issued_at` (예측에 쓴 데이터의 마지막 시각), `*_margin_mw` (90% 구간 반폭, config 면 `null`).

응답 (배열은 24개 중 앞부분만):

```json
{
  "horizon_h": 24, "n_samples": 300, "seed": 42,
  "base": { "demand_mw": [51901.5, 49795.8], "solar_mw": [3.8, 3.8], "wind_mw": [179.0, 209.0],
            "demand_margin_mw": [4396.3, 4396.3], "solar_margin_mw": [15.1, 13.7],
            "wind_margin_mw": [194.0, 214.7],
            "start": "2026-09-28T00:00:00+09:00", "issued_at": "2026-09-27T11:00:00+09:00",
            "source": "forecast_api" },
  "scenarios": [
    { "scenario_id": "S01", "probability": 0.06, "label": "고수요",
      "peak_net_load_mw": 75921.5, "demand_dev_pct": 4.67, "solar_dev_pct": -10.48, "wind_dev_pct": -10.57,
      "demand_mw": [51112.5, 49630.4], "solar_mw": [0.0, 3.1], "wind_mw": [264.5, 245.5] }
  ],
  "net_load_band": { "p5": [47719.8, 45733.8], "p50": [51597.6, 49508.3], "p95": [55322.6, 53710.9] },
  "demand_band":   { "p5": [47887.2, 45959.0], "p50": [51769.5, 49730.3], "p95": [55477.0, 53911.3] },
  "checks": [
    { "name": "확률 합 = 1", "ok": true, "detail": "1.000000" },
    { "name": "수요 표본의 예측구간 적중률 ≈ 90%", "ok": true,
      "detail": "표본 x 시간 중 forecast_api 90% 구간 안 90.3% (허용 ±3%p)" }
  ]
}
```

(2026-11-03 로컬 실행값, `{}` 요청. 숫자는 소수 일부를 줄였다.)

`checks` 항목: 시나리오 수 ≥ 10 · 확률 합 = 1 · 수요 표본 편향 ≤ 1% · 수요 표본의 예측구간 적중률 (구간이
있을 때만, 목표 = 2Φ(1.645 / `sigma_scale`) − 1) · 축약 후 기대 순부하 보존 ≤ 1% · 상방/하방 꼬리 보존 ·
물리 범위. **꼬리 보존 두 항목은 forward selection 이 꼬리를 일부러 남기지 않아 실패할 수 있다**
(seed 30개 x K 10·15 에서 상방 23/60, 하방 17/60 실패, 2026-11-03). 실패도 그대로 보고한다.

`scenarios` 는 피크 순부하 내림차순이다 (S01 이 가장 빡빡한 날). 각 시나리오의 `scenario_id`·`probability`·`demand_mw`·`solar_mw`·`wind_mw` 를 그대로 `/dispatch/stochastic` 의 `scenarios` 로 넘기면 된다.

## POST /api/v1/dispatch/stochastic (dispatch_api `:8002`)

Two-Stage Stochastic 급전 계획과 VSS. 스키마: `shared/schemas.py` 의 `StochasticRequest` / `StochasticResponse`.
정식화는 [formulation.md](formulation.md) 8절, VSS·EVPI 정의는 9절.

한 번 호출하면 RP(전 시나리오) → EV(평균 시나리오 1개) → EEV(EV 의 1단계 해 고정) 순으로 풀고,
`compute_ws` 가 켜져 있으면 WS(시나리오마다 1개씩) 를 더 푼다. `solver` 의 시간 제한·gap 은 **문제마다**
적용된다.

> **기본값은 시간 제한 없음이다** (`config/stochastic.yaml` 의 `request_defaults.time_limit: null`).
> 21기 x 시나리오 10개에서 RP·EV·EEV 를 모두 푸는 데 1~2분 걸린다 (2026-11-12 로컬 측정, forecast_api
> 기준 예측: 빈 요청(gap 0.5%) 77초, `precompute` 조건(gap 0.1%) 113초 — 113초는 다른 계산과 CPU 를 나눠 쓴 값).
> WS 를 켜면 시나리오 수만큼 더 걸린다. 시간 제한을 주면 RP 가 덜 풀려 VSS 가 음수로 나올 수 있다
> (예전 측정: 10초 제한에서 `status: time_limit`, `vss_pct` −36.8%). 대시보드 VSS 타일은 미리 계산해 둔
> `GET /dispatch/stochastic/latest` 를 쓴다.
> 기본 gap 0.5% 는 VSS(3% 안팎)에 비해 작지 않다. VSS 를 판정할 때는 `mip_gap` 을 0.001 이하로 준다.

요청 — 모든 필드를 비울 수 있다 (`{}`).

기본값 열의 숫자는 현재 `config/stochastic.yaml` 값이다. 필드를 비우면(`null`) 서버가 config 에서 채운다.

| 필드 | 기본값 | 설명 |
|---|---|---|
| `start` | `null` | 계획 시작 시각 (응답 timestamp 의 기준). `scenarios`·`base` 를 모두 비워 forecast_api 예측을 쓰면 **예측 구간의 시작 시각을 쓰고 이 값은 무시한다**. 그 밖에 `null` 이면 오늘 0시 (KST) |
| `horizon_h` | `null` | 아직 쓰지 않는다. 계획 길이는 시나리오 배열 길이다 |
| `scenarios` | `null` | 시나리오 직접 입력. `null` 이면 `/scenarios` 와 같은 생성기로 `n_scenarios` 개를 만든다 (7절 파라메트릭 오차 모델) |
| `n_scenarios` | `null` → 10 (`scenario_generator.n_scenarios`) | 생성 개수 (`scenarios` 가 `null` 일 때만) |
| `seed` | `null` → 42 (`scenario_generator.seed`) | 생성 seed. 0 도 그대로 쓴다 |
| `base` | `null` | 생성용 기준 예측 (`ScenarioProfile`). `null` 이면 forecast_api 예측, 실패하면 `base_profile` (`/scenarios` 의 "기준 예측") |
| `compute_ws` | `false` | `true` 면 WS·EVPI 도 계산. 시나리오 수만큼 더 풀어 느려진다 |
| `generator_ids` | `null` | 사용할 유닛 id. `null` 이면 `generators.yaml` 전체 |
| `use_ess` | `true` | **현재 무시된다.** 2단계에 ESS 를 넣지 않았다 (8절 끝, 🟨 C 와 상의) |
| `reserve_ratio` | `null` → 0.0 (`request_defaults.reserve_ratio`) | 1단계 예비율 $r$. 요건은 $(1 + r) \times$ 기대 순수요 $+$ `reserve_mw` 다. 부족분은 슬랙으로 허용하고 `config/stochastic.yaml` 의 페널티를 붙인다 (가정, 8.4 S5) |
| `reserve_mw` | `null` → 700 (`request_defaults.reserve_mw`) | 1단계 고정 예비력 $R$ (MW). 값은 가정이다 (8.2) |
| `shed_penalty_won_per_mwh` | `null` → 3,093,000 (`request_defaults.shed_penalty`) | 부하차단 페널티 $K$. VoLL 3,093원/kWh (KEEI 기본연구보고서 24-21) |
| `include_scenario_hours` | `false` | `true` 면 `scenarios[].hours` 에 시나리오별 시간 요약을 넣는다 |
| `solver` | `null` → `{null, 0.005}` (`request_defaults.time_limit`·`mip_gap`) | 문제당 시간 제한(초, `null` = 제한 없음)·상대 gap. 위 경고 참고. `solver` 를 주면서 일부만 채우면 빠진 값은 `SolverOptions` 스키마 기본값(10초, 0.005)이다 — 제한 없이 풀려면 `"time_limit_s": null` 을 적는다 |

```json
{
  "start": null,
  "horizon_h": 24,
  "scenarios": null,
  "n_scenarios": 10,
  "seed": 1,
  "base": null,
  "compute_ws": false,
  "generator_ids": null,
  "reserve_ratio": 0.0,
  "reserve_mw": 700,
  "shed_penalty_won_per_mwh": 3093000,
  "include_scenario_hours": false,
  "solver": { "time_limit_s": null, "mip_gap": 0.001 }
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
| `expected_cost` | 시나리오 비용의 확률 가중 평균 (= RP 기대비용). `fuel_won` = 증분 연료비 + 무부하비 (8.3), `startup_won` = 1단계 기동비 + 당일 추가 기동비, `recourse_won` = 예비력 부족 페널티 (가정 항, 모든 시나리오에 같다), `shed_won` = 부하차단 페널티 |
| `expected_hours` | 시간별 수급의 확률 가중 평균. `reserve_pct` 의 공급능력은 가동 유닛 상한 합 + 태양광 + 풍력. `ess_mw` 는 항상 0 |
| `scenarios[]` | 시나리오별 `cost`, `shed_mwh`, `rt_adjust_mwh` (기대 출력 대비 그 시나리오 출력의 편차 $\sum\lvert P_s - E[P]\rvert$), `hours` |
| `vss` | `rp_won`·`ev_won`·`eev_won` 은 각 문제의 목적값. `vss_won` = EEV − RP, `vss_pct` = `vss_won` / `eev_won` x 100 (분모는 가정, 9절), `target_met` = `vss_pct` ≥ 3. `ws_won`·`evpi_won` 은 `compute_ws` 일 때만 |
| `start` | 실제로 쓴 계획 시작 시각 (forecast_api 예측을 쓰면 예측 구간의 시작) |
| `base_source` | 시나리오의 기준 예측 출처. `forecast_api` / `config` (`base_profile` 폴백) / `request` (요청이 `scenarios` 나 `base` 를 줌) |

오류:

| 코드 | 경우 |
|---|---|
| 422 | `start` 누락, 시나리오 길이 불일치, 선택된 발전기 없음 (`generator_ids` 가 모두 없는 id), 입력값 오류 |
| 500 | RP·EV·EEV 중 하나의 해를 찾지 못함 (infeasible 또는 시간 안에 해 없음). EEV 는 평균 계획의 최소출력 합이 낮은 시나리오의 수요를 넘으면 infeasible 이다 (과잉 발전, 8.4 S1) |

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
  },
  "base_source": "forecast_api"
}
```

## GET /api/v1/dispatch/stochastic/latest

대시보드 VSS 타일용. dispatch_api 가 켜질 때와 `refresh_hours` 마다 백그라운드로 Two-Stage 를 한 번 풀어 두고,
이 엔드포인트는 마지막 결과를 바로 돌려준다 (풀이 자체는 1분 안팎). 계산 조건은 `config/stochastic.yaml` 의
`precompute` 블록이다 (seed 1, 시나리오 10개, 예비력 700MW, 시간 제한 없음, gap 0.1%). 기준 예측은
`/scenarios` 와 같이 forecast_api 에서 받고 (실패하면 `base_profile`), 계획 구간은 그 예측 구간이다
(`result.start`, `result.base_source`). `base_profile` 로 폴백한 결과면 (`base_source: "config"`)
`retry_interval`(5분)마다 forecast_api 를 다시 불러 보고, 응답이 오면 그때 다시 푼다. 환경변수 `PRECOMPUTE_ENABLED=false` 면 계산하지 않는다.

스키마: `shared/schemas.py` 의 `StochasticLatest`.

| 필드 | 설명 |
|---|---|
| `state` | `pending` 시작 전 · `running` 계산 중 · `done` · `error` · `disabled` |
| `computed_at` | 마지막으로 성공한 계산 시각 (KST) |
| `seed`, `n_scenarios`, `reserve_ratio`, `reserve_mw`, `shed_penalty_won_per_mwh`, `time_limit_s`, `mip_gap` | 계산 조건. `time_limit_s` 가 `null` 이면 제한 없음 |
| `result` | `StochasticResponse`. 아직 없으면 `null`. 계산 중이거나 실패해도 이전 결과는 남는다 |
| `error` | 마지막 실패 메시지 |

```json
{
  "state": "done", "computed_at": "2026-11-02T21:10:00+09:00",
  "seed": 1, "n_scenarios": 10, "reserve_ratio": 0.0, "reserve_mw": 700.0,
  "shed_penalty_won_per_mwh": 3093000.0,
  "time_limit_s": null, "mip_gap": 0.001,
  "result": { "status": "optimal", "solve_time_s": 34.5, "start": "2026-09-27T01:00:00+09:00",
              "base_source": "forecast_api", "vss": { "vss_pct": 0.0, "...": "..." }, "...": "..." },
  "error": null
}
```
