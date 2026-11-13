# VPP 전력 수급 최적화

한국 전력시장(CBP) 데이터를 공공 API로 수집해 전력 수요·태양광·풍력을 예측하고, 그 결과로 기동정지(UC) 포함 경제급전(ED)을 MILP로 풀어 Rule-based 대비 비용·탄소·ESS 피크 감소 효과를 비교하는 시스템이다. 선택 과제는 **A. Two-Stage Stochastic (VSS)** 를 구현했다.

모든 서비스는 `docker-compose up` 한 번으로 뜨고, 서비스끼리는 서로의 코드를 import하지 않고 REST(HTTP)로만 통신한다.

## 구현 현황

| 모듈 | 상태 | 위치 |
| --- | --- | --- |
| 데이터 수집 (KPX·SMP·기상청·K-water) | 구현 | `services/collector` |
| 시계열 예측 `POST /api/v1/forecast` | 구현 (LightGBM 서빙, LSTM은 학습·비교만) | `services/forecast_api` |
| MILP 경제급전 `POST /api/v1/dispatch/milp` | 구현 (Rule-based 비교·ESS·탄소 포함) | `services/dispatch_api` |
| 시나리오 생성 `POST /api/v1/scenarios` | 구현 (300개 표본 → 10개 축약) | `services/dispatch_api/scenarios.py` |
| 선택 A: Two-Stage Stochastic + VSS | 구현 | `services/dispatch_api/stochastic.py` |
| 선택 B: RL 급전 에이전트 | 미구현 | - |
| 위기 시나리오 시뮬레이터 | 미구현 (대시보드 패널은 목업) | `docs/formulation.md` 10장 |
| 통합 관제 대시보드 (Streamlit) | 구현 | `services/dashboard` |

## 시스템 아키텍처
```
                Public API (KPX / data.go.kr / KMA / K-water)
                                      │
                                      ▼
                              collector (8000) ◀──────────── REST ──────────────┐
                                      │                                         │
                                      │ write                                   │
                                      ▼                                         │
                                   data/csv                                     │
                                      │ read                                    │
                      ┌───────────────┴───────────────┐                         │
                      ▼                               ▼                         │
              forecast_api (8001) ◀── REST ── dispatch_api (8002) ── REST ──────┤
                      ▲                               ▲                         │
                      │ REST                          │ REST                    │
                      └───────────────┬───────────────┘                         │
                                      │                                         │
                              dashboard (8501) ───────────────────────── REST ──┘
```

- **collector**: 기동 시 `data/csv`를 메모리에 올리고 `GET /data/{name}`으로 내보낸다. `COLLECT_ENABLED=true`면 마지막 시각 이후 결측 구간을 API로 백필하고 `INTERVAL_SEC`(기본 300초)마다 반복한다. 기본값은 `false`라 API 키 없이도 CSV 스냅샷만으로 전체 시스템이 뜬다.
- **forecast_api**: `data/csv`를 직접 읽어 피처를 만들고 저장된 LightGBM 모델(`models/saved/`)로 예측한다. 요청마다 재학습하지 않는다.
- **dispatch_api**: 급전 입력(수요·발전원별 출력·댐 저수율)은 `data/csv`를, 발전기·ESS·솔버 파라미터는 `config/`를 읽는다. 시나리오 기준 예측은 forecast_api를 REST로 부르고, 실패하면 `config/stochastic.yaml`의 `base_profile`로 폴백한다. 연료비는 collector REST → 실패 시 CSV 순으로 읽는다. 기동 시 백그라운드 스레드가 Two-Stage 결과를 미리 계산해 `/latest`로 제공한다.
- **dashboard**: 설정 외의 모든 데이터를 세 서비스의 REST로 받는다. 응답이 없거나 아직 계약이 없는 패널은 목업으로 그리고 `목업` 태그를 단다.

## 실행 방법

요구 환경: Docker 24.0+, Docker Compose 2.0+

```bash
cp .env.example .env
docker-compose up --build
```

| 서비스 | 포트 | 확인 주소 |
| --- | --- | --- |
| collector | 8000 | http://localhost:8000/health |
| forecast_api | 8001 | http://localhost:8001/health |
| dispatch_api | 8002 | http://localhost:8002/health |
| dashboard | 8501 | http://localhost:8501 |

## API

요청/응답 JSON 샘플은 [docs/api_spec.md](docs/api_spec.md)에 있다.

| 서비스 | 메서드 · 경로 | 설명 |
| --- | --- | --- |
| collector | `GET /health` | 상태 + 데이터셋별 행 수 |
| collector | `GET /datasets` | 데이터셋 정의 (컬럼·키·간격·행 수) |
| collector | `GET /data/{name}?start=&end=&limit=` | 데이터셋 행 조회 |
| forecast_api | `POST /api/v1/forecast` | 수요·태양광·풍력 24~168h 예측 + 90% 예측구간 |
| dispatch_api | `POST /api/v1/dispatch/milp` | UC+ED MILP 급전 계획, Merit Order, 비용, 탄소, ESS, 제약 검증, Rule-based 비교 |
| dispatch_api | `POST /api/v1/scenarios` | 수요·태양광·풍력 불확실성 시나리오와 확률 |
| dispatch_api | `POST /api/v1/dispatch/stochastic` | Two-Stage 급전 계획 + VSS (RP·EV·EEV, 선택 시 WS·EVPI) |
| dispatch_api | `GET /api/v1/dispatch/stochastic/latest` | 서버가 미리 계산한 최신 Two-Stage 결과 |

`POST /api/v1/dispatch/milp`는 모든 필드에 기본값이 있어 `{}`만 보내도 수집 데이터의 최신 연속 24시간으로 푼다.

## 데이터

`data/csv`에 CSV 스냅샷이 커밋되어 있어 API 장애·키 없음 상태에서도 실행된다. 기간은 저장소의 현재 스냅샷 기준이다.

| 데이터셋 | 출처 | 간격 | 스냅샷 기간 |
| --- | --- | --- | --- |
| `power_demand` | KPX 전력수급현황 (`sukub.do`) — 현재수요·공급능력 | 5분 | 2023-01-01 ~ 2026-11-08 |
| `trading_by_fuel` | data.go.kr `getPowerTradingResultInfo1` — 연료원별 설비용량·거래량 | 1시간 | 2023-01-01 ~ 2026-10-10 |
| `smp` | data.go.kr `getSmpWithForecastDemand` (육지) | 1시간 | 2025-07-01 ~ 2026-11-08 |
| `weather` | 기상청 API허브 `kma_sfctm3` — 기온·습도·풍속·일사량, 13개 지점 | 1시간 | 2023-01-01 ~ 2026-11-08 |
| `dam_status` | K-water `sluicePresentCondition/mntlist` — 소양강댐 수위·유입·방류·저수율 | 10분 | 2025-07-01 ~ 2026-11-08 |
| `fuel_cost` | 연료원별 월 단가 (수집 API 없음, CSV로만 관리) | 월 | 2025-07 ~ 2026-08 |

파생 변수(CDD·HDD, 요일·주말·공휴일 플래그, 기상 이동평균)는 forecast_api의 `features.py`에서 만든다. 소스별 상세는 [docs/data_sources.md](docs/data_sources.md).

## 모듈 상세

### 예측 (`services/forecast_api`)

- 수요: 과거값 lag + 기온·CDD/HDD + 달력 피처로 1시간씩 이어서 예측 (recursive)
- 태양광·풍력: 기상 + 달력 피처로 각 시각을 바로 예측 (direct). 태양광은 이용률로 학습 후 설비용량을 곱한다
- 90% 예측구간: Conformal Prediction (calibration 4주 구간의 오차 분위수)
- 학습은 오프라인 스크립트(`models/train_lgbm.py`, `models/train_lstm.py`)로 하고 결과를 `models/saved/`에 저장한다. 데이터 분할은 시간순 train / calib(4주) / test(2주)
- 미래 기상은 기상청 예보 API를 아직 붙이지 않아 해당 시각 관측값으로 대신하고, 관측값도 없으면 시간대별 과거 평균으로 채운다

세 대상 모두 LightGBM이 더 정확해 API는 LightGBM으로 서빙한다 (`model: "lstm"`은 501).

### MILP 경제급전 (`services/dispatch_api`)

- PuLP + HiGHS. 발전기는 `config/generators.yaml`의 대표 유닛(현재 원자력 4, 유연탄 5, 무연탄 2, LNG 5, 수력 5 = 21기)
- 목적함수: 연료비 + 기동비 최소화
- 제약: 수급 균형, 예비율 ≥ 10%, 출력 상하한, 램프업/다운, 최소 기동/정지 시간, 수력 저수율 30% 미만 시 출력 제한
- 해를 받은 뒤 `validation.py`가 모든 제약을 다시 검사해 응답 `constraints`에 싣는다
- Rule-based 베이스라인(`baseline.py`): 연료비 오름차순 우선순위, 매 시간 독립 결정, ESS·예측 미사용. 같은 비용 회계로 절감률을 계산한다
- ESS(`config/ess.yaml`): 충방전 4,700MW, 용량 32,900MWh, 왕복효율 75%, SOC 10~95%. 모드 `cost` / `peak_shaving` / `combined`
- 탄소(`carbon.py`): 발전원별 배출계수로 시간대별·발전원별·유닛별 배출량과 배출집약도 계산
- 수식과 기호↔코드 매핑은 [docs/formulation.md](docs/formulation.md) 4~6장, 11장

### Two-Stage Stochastic (선택 A)

- Pyomo + HiGHS
- 1단계(Day-ahead): 시나리오와 무관한 발전기 기동/정지 결정
- 2단계(Real-time): 시나리오별 출력과 부하차단
- 시나리오: forecast_api 예측과 90% 구간을 기준으로 AR(1) 오차를 갖는 표본 300개를 뽑고 fast forward selection으로 10개로 축약
- VSS = EEV − RP, `vss_pct` = VSS / EEV × 100
- 정식화와 측정 기록은 [docs/formulation.md](docs/formulation.md) 7~9장

### 대시보드 (`services/dashboard`)

| 페이지 | 내용 |
| --- | --- |
| 통합 관제 | 수요·공급·예비율 게이지와 알람(7% 미만 Warning, 5% 미만 Critical), Merit Order, 최적화 결과, 탄소 추이, ESS, 위기 시나리오(목업) |
| 분석 상세 | 168h 예측·성능 지표, 최적화 상세, 위기 시나리오 게이지, 배출 내역 |
| 시간대 데이터 | KPX 실측, 출력 히트맵, 시간대 상세표·CSV |
| 시나리오 생성기 | `POST /api/v1/scenarios` 결과 확인 |
| Two-Stage 테스트 | RP·EV·EEV·WS 비교, VSS 판정 |

판정 기준·알람 임계값·목업 값은 `config/dashboard.yaml`에 있다.

## 개발

```bash
pip install -e ".[dev]"
ruff check .
pytest            # --cov=services 가 pyproject.toml 에 설정되어 있다
```

GitHub Actions(`.github/workflows/ci.yml`)가 push·PR마다 Python 3.11에서 `ruff check .`와 `pytest`를 돌린다.

## 디렉터리 구조

```
.
├── docker-compose.yml
├── pyproject.toml            # 전체 의존성 + ruff/pytest 설정
├── .env.example
├── config/
│   ├── settings.yaml         # 급전 horizon, 솔버, 예비율, 수력 제약
│   ├── generators.yaml       # 발전기 유닛 파라미터
│   ├── ess.yaml              # ESS 사양·운전 모드
│   ├── stochastic.yaml       # 시나리오 생성기, Two-Stage, 미리 계산
│   └── dashboard.yaml        # 알람·KPI 기준, 목업 값
├── data/csv/                 # 수집 데이터 스냅샷
├── docs/
│   ├── formulation.md        # 수학적 정식화
│   ├── api_spec.md           # 엔드포인트 요청/응답 샘플
│   └── data_sources.md       # 데이터 출처
├── scripts/measure_dispatch_kpi.py
├── shared/                   # 공용 스키마(schemas.py)·상수
├── services/
│   ├── collector/            # 수집기(sources/) + data_api
│   ├── forecast_api/         # 예측 API + 학습 스크립트·저장 모델
│   ├── dispatch_api/         # MILP·Rule-based·ESS·탄소·시나리오·Stochastic
│   └── dashboard/            # Streamlit
└── tests/
```

## 팀원별 기여

| 이름 | 역할 | 담당 내용 |
| --- | --- | --- |
| 이권희 | A 데이터·인프라 | collector 수집 파이프라인(KPX·data.go.kr·기상청·K-water), CSV 스냅샷과 data_api, `docs/data_sources.md`, 발전기 데이터 |
| 최진솔 | B 예측 | forecast_api, LightGBM·LSTM 학습과 비교, Conformal 예측구간, 피처 엔지니어링 |
| 육나윤 | C 최적화 | UC/ED MILP, Rule-based 베이스라인, ESS, 탄소 계산, 제약 사후 검증, config 로더, KPI 측정 스크립트, CI |
| 민도련 | D 확률·문서·통합 | 시나리오 생성기, Two-Stage Stochastic·VSS, 미리 계산, forecast_api 연동, 대시보드, `formulation.md`·`api_spec.md` |

### 탄소 감축 분석

대시보드 **탄소 감축 분석** 페이지에서 감축 목표와 ESS 사용 여부를 선택하고 실행한다.
같은 수요·예비율에서 비용 최소 MILP와 탄소 상한 계획의 비용, 발전원별 발전량,
유닛별 출력·가동 시간·기동 횟수, ESS 운전을 비교한다. 부하차단은 허용하지 않는다.
이 화면은 API 실패 시 목업으로 대체하지 않는다.

- `POST /api/v1/dispatch/milp`: 선택 `carbon_cap_ton`으로 배출 상한 적용(기본 off).
- `POST /api/v1/dispatch/pareto`: 비용–탄소 표본과 목표 감축률의 실현 가능성 계산.
- 새 분석 계수: `config/carbon.yaml`. 급전 가능한 발전원의 전과정 배출 범위 중간값을
  사용하는 시나리오이며 국내 발전소 실측·공식 감축 실적이 아니다. 기존 기본 계수는 유지한다.
- 반복 측정: `python scripts/measure_carbon_kpi.py --windows 3 --json /tmp/carbon.json`.
- 계약·해석: `docs/api_spec.md`, `docs/formulation.md` 6.1–6.2.
