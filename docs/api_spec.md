# API 명세 (API Spec)

## collector data_api (`:8000`)

수집한 원본을 그대로 내보낸다. 시각은 `"2026-09-22 15:55:00"` (`fuel_cost` 만 `"2026-09"`),
빈 값은 `null`, 응답은 시간 오름차순.

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

## TODO: POST /api/v1/forecast

## TODO: POST /api/v1/dispatch/milp

## TODO: POST /api/v1/dispatch/stochastic
