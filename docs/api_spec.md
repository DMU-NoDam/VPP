# API 명세 (API Spec)

## POST /api/v1/forecast

수요·태양광·풍력 시계열 예측 API (forecast_api, 포트 8001). 향후 24~168시간의 예측값과
90% 예측구간(conformal)을 반환한다.

### 요청

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `start_date` | string (YYYY-MM-DD) | O | 학습/조회용 과거 데이터 시작일 |
| `end_date` | string (YYYY-MM-DD) | O | 과거 데이터 종료일 |
| `target` | `"demand"` \| `"solar"` \| `"wind"` \| `"all"` | O | 예측 대상. `all`이면 세 개를 한 번에 반환 |
| `horizon_hours` | integer (24~168) | O | 예측 시간 범위 |
| `model` | `"auto"` \| `"lightgbm"` \| `"lstm"` | X (기본 `auto`) | `auto`는 현재 lightgbm으로 동작 |
| `pi_method` | `"conformal"` \| `"quantile"` | X (기본 `conformal`) | 현재 conformal만 구현됨 |

```json
{
  "start_date": "2025-06-01",
  "end_date": "2026-01-01",
  "target": "all",
  "horizon_hours": 24,
  "model": "auto",
  "pi_method": "conformal"
}
```

### 응답 (200)

```json
{
  "result_code": "00",
  "result_msg": "OK",
  "generated_at": "2026-09-29T10:00:00",
  "horizon_hours": 24,
  "forecasts": {
    "demand": {
      "model_used": "lightgbm",
      "pi_method": "conformal",
      "metrics": {
        "mape": 2.45,
        "nmae": 2.38,
        "coverage": 89.9,
        "primary_value": 2.45
      },
      "points": [
        {
          "datetime": "2026-01-01T01:00:00",
          "predicted": 44536.3,
          "lower_bound": 41959.7,
          "upper_bound": 47112.9
        }
      ]
    },
    "solar": { "...": "demand와 동일 구조 (metrics.primary_value는 nMAE 기준)" },
    "wind": { "...": "demand와 동일 구조 (metrics.primary_value는 nMAE 기준)" }
  }
}
```

`metrics`는 요청마다 즉석 계산이 아니라, calibration 구간(과거 데이터의 마지막 15%)으로
미리 계산해둔 값이다. `primary_value`는 타겟별 스펙 판단 기준 지표(수요=mape,
태양광/풍력=nmae)를 가리킨다.

### 에러 응답

| result_code | HTTP status | 상황 |
|---|---|---|
| `21` | 500 | 모델 파일 없음 (`MODEL_LOAD_ERROR`, 예: `models/train_lgbm.py` 미실행) |
| `21` | 501 | 요청한 모델(`lstm`) 서빙 로직 미구현 (`MODEL_NOT_IMPLEMENTED`) |
| - | 422 | 요청 파라미터 검증 실패 (FastAPI/pydantic 기본 에러 형식) |

```json
{
  "detail": {
    "result_code": "21",
    "result_msg": "MODEL_LOAD_ERROR",
    "detail": ".../models/saved/demand_lgbm.txt 없음 — models/train_lgbm.py 먼저 실행 필요"
  }
}
```

### 참고

- 발전원별 예측 대상 중 `wind`는 실제 KPX 데이터에 "풍력" 단독 필드가 없어
  `generation_by_fuel`의 `renewable`(신재생) 값을 임시로 대체 사용 중 (검증 필요, `data_client.py` 주석 참고)
- 실데이터가 10일(240시간) 미만이면 자동으로 목업 데이터로 대체됨 (`data_client.py`)

## TODO: POST /api/v1/dispatch/milp

## TODO: POST /api/v1/dispatch/stochastic
