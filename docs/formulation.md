# 최적화 모델 정식화 (Formulation)

> 목차 초안. 각 절의 담당은 `[C]` 최적화 코어, `[D]` 확률론적 최적화.
> 절마다 "무엇을 적을지"만 한 줄씩 적어 두었다. 수식은 3주차부터 채운다.

## 0. 개요

- 풀려는 문제: 24h(1시간 단위) Unit Commitment + Economic Dispatch
- 접근 3종과 관계: Rule-based(베이스라인) → 결정론적 MILP → Two-Stage Stochastic
- API 매핑: `/dispatch/milp`, `/dispatch/stochastic` ([api_spec.md](api_spec.md))

## 1. 표기법 `[C][D]`

### 1.1 집합

- TODO: 유닛 G (발전원별 부분집합 G_nuc, G_coal, G_lng, G_hydro), 시간 T, 시나리오 S

### 1.2 파라미터

- TODO: Pmin/Pmax, 연료비, 기동비, 램프율, 최소 기동/정지 시간, 배출계수, 예비율, ESS 사양, 저수율

### 1.3 결정변수

- TODO: u (기동), v/w (기동/정지 이벤트), p (출력), ESS 충/방전·SoC, 부하차단 슬랙

## 2. 파라미터 출처와 유닛 모델링 `[C]`

- TODO: EPSIS 값 → `config/generators.yaml` 매핑표, 출처·기준일
- TODO: 발전원별 4~10개 블록 분할 규칙 (총 20~40 유닛)
- TODO: 설비용량 검산 (스펙 표 합계 106GW vs 여름 피크)

## 3. Rule-based 베이스라인 `[C]`

- TODO: 규칙 명시 — 연료비 우선순위 급전, 고정 예비율 확보, 무예측·무ESS
- TODO: 비용 절감률 정의식 `(C_rule - C_milp) / C_rule`

## 4. 결정론적 UC/ED MILP `[C]`

### 4.1 목적함수

- TODO: 연료비 + 기동비 (+ 부하차단 페널티)

### 4.2 제약조건

1. TODO: 수급 균형
2. TODO: 예비율 ≥ 수요의 10%
3. TODO: 출력 상하한 `u·Pmin ≤ p ≤ u·Pmax`
4. TODO: 램프율 (Ramp-up / Ramp-down, 기동·정지 시점 처리)
5. TODO: 최소 기동/정지 시간 (Minimum Up/Down Time)
6. TODO: 수력 저수율 < 30% 일 때 출력 제한
7. TODO: 기동/정지 이벤트 연결식 `v - w = u_t - u_{t-1}`

### 4.3 부하차단 슬랙과 페널티

- TODO: 용량 부족 시 infeasible 대신 부족량이 숫자로 나오게 하는 설계, 페널티 값 근거

## 5. ESS 연계 `[C]`

- TODO: SoC 동역학, Round-trip 효율(0.85) 반영 방식 (충전/방전 효율 분할)
- TODO: 충·방전 동시 금지 처리
- TODO: 피크 감소율 정의식 (원 수요 피크 vs ESS 반영 순부하 피크), ESS 규모 가정

## 6. 탄소 배출 `[C]`

- TODO: 시간대별 배출량 `Σ e_g · p_{g,t}`, 배출계수 출처
- TODO: (보너스) 비용-탄소 가중합 / ε-constraint 로 Pareto Front

## 7. 시나리오 생성 `[D]`

구현: `services/dispatch_api/scenarios.py`, API `POST /api/v1/scenarios`, 확인 화면은 대시보드
"시나리오 생성기" 페이지.

### 7.1 범위

- 불확실성 변수는 수요 $D_{t}$, 태양광 $S_{t}$, 풍력 $W_{t}$ ($t = 1..24$) 세 가지다.
- 발전기 고장은 넣지 않는다. 이산 사건이라 위기 시나리오(10장)와 겹치고, VSS 가 고장 운에 좌우된다.
- 위기 시나리오(폭염 +15% 등)도 넣지 않는다. 90% 예측구간 밖이라 확률 가중을 왜곡한다.

### 7.2 오차 모델 (파라메트릭 — 1년치 잔차 확보 후 일 단위 블록 부트스트랩으로 교체)

기준 예측 $\bar D_t, \bar S_t, \bar W_t$ 에 대해, 표본 $n = 1..N$ 마다

$$\varepsilon_{n,t} = \phi\,\varepsilon_{n,t-1} + \sigma\sqrt{1-\phi^2}\,z_{n,t},\qquad z \sim \mathcal N(0,1)$$

| 변수 | 식 | 기본값 |
|---|---|---|
| 수요 | $D_{n,t} = \bar D_t (1 + \varepsilon^{D}_{n,t})$ | $\sigma_D = 2.5\%$, $\phi_D = 0.9$ |
| 태양광 | $S_{n,t} = \mathrm{clip}\big(\bar S_t\, c_n (1 + \varepsilon^{S}_{n,t}),\ 0,\ \bar S^{\max}\big)$, $c_n = 1 + m - X_n$, $X_n \sim \Gamma(0.8,\ m/0.8)$ | $m = 10\%$, $\sigma_S = 8\%$, $\phi_S = 0.7$ |
| 풍력 | $W_{n,t} = \mathrm{clip}\big(\bar W_t\, e^{\varepsilon^{W}_{n,t} - \sigma_W^2/2},\ 0,\ \bar W^{\max}\big)$ | $\sigma_W = 25\%$, $\phi_W = 0.85$ |

- 흐림 계수 $c_n$ 은 하루 공통이고 평균이 1 이다. 감마 꼬리 때문에 **하방으로 치우친다** (흐린 날은 가끔, 깊게).
- 순부하는 $L_{n,t} = D_{n,t} - S_{n,t} - W_{n,t}$ 이다.

### 7.3 축약: N = 300 → K = 10, 꼬리 보존

1. **상방 꼬리 군집**: 피크 순부하 $\max_t L_{n,t}$ 가 상위 10% 인 표본들.
2. **하방 꼬리 군집**: 최저 순부하 $\min_t L_{n,t}$ 가 하위 10% 인 표본들 (과발전·재생 감발 위험).
3. 나머지 표본은 순부하 경로 $L_{n,\cdot} \in \mathbb R^{24}$ 의 유클리드 거리로 k-medoids 를 돌려 $K-2$ 군집으로 묶는다.
4. 군집마다 medoid 를 대표 시나리오 $s$ 로 두고, 확률은 $\pi_s = |\text{군집}_s| / N$ 이다.

k-medoids 만 쓰면 대표가 중앙으로 끌려가 꼬리가 사라지고, 그러면 VSS 가 작게 나온다. 꼬리 군집 없이 돌렸을 때는 seed 1·7 에서 상방 꼬리 보존이 실패했다. 꼬리 군집을 넣은 뒤에는 seed 0~199 × K ∈ {10, 12, 20} 에서 7.4 의 검증이 모두 통과했다.

### 7.4 검증 (생성할 때마다 응답의 `checks` 로 반환)

| 항목 | 기준 |
|---|---|
| 시나리오 수 | $K \ge 10$ |
| 확률 합 | $\sum_s \pi_s = 1$ |
| 수요 표본 편향 | 표본 평균의 일간 에너지가 기준 예측과 1% 이내 |
| 축약 후 기대 순부하 보존 | $\sum_s \pi_s \sum_t L_{s,t}$ 가 표본 평균과 1% 이내 |
| 상방 꼬리 보존 | $\max_s \max_t L_{s,t} \ge$ 표본 피크의 P90 |
| 하방 꼬리 보존 | $\min_s \min_t L_{s,t} \le$ 표본 최저의 P10 |
| 물리 범위 | 태양광 야간 0, $0 \le S \le \bar S^{\max}$, $0 \le W \le \bar W^{\max}$ |

### 7.5 라벨 (설명용)

시나리오마다 기준 예측 대비 일간 에너지 편차로 이름을 붙인다: 수요 ±1.5% (고수요/저수요), 태양광 −15% 흐림 / +5% 맑음, 풍력 ±20% (강풍/약풍). 해당 없으면 "기준 근접"이다.

## 8. Two-Stage Stochastic `[D]`

### 8.1 단계 구분

- TODO: 1단계(Day-ahead) — 기동계획 u, DA 출력 계획
- TODO: 2단계(Real-time) — 시나리오별 출력 조정, 속응 LNG 추가 기동, ESS, 부하차단

### 8.2 목적함수 (확장형, Deterministic Equivalent)

- TODO: 1단계 비용 + Σ_s π_s · 2단계 비용

### 8.3 제약조건

- TODO: 시나리오별로 복제되는 4장 제약
- TODO: 비예측성(Non-anticipativity) — 1단계 변수 공유
- TODO: 실시간 조정 비용 구조 (상향/하향 단가 프리미엄) — VSS 크기를 좌우하는 부분

## 9. VSS / EVPI `[D]`

- TODO: RP, EV, EEV, WS 정의와 계산 절차
- TODO: `VSS = EEV - RP`, `VSS% = VSS / EEV × 100` (기준 ≥ 3%)
- TODO: `EVPI = RP - WS`
- TODO: 결과표와 해석

## 10. 위기 시나리오 `[C]`

- TODO: 폭염(수요 +15%), 발전소 탈락(1,400MW), 태양광 50% 램프다운 — 각각 파라미터 변경 방식
- TODO: 예비율 5% 유지 판정

## 11. 구현 매핑 `[C][D]`

- TODO: MILP — PuLP + HiGHS/CBC, MIP gap·time limit (응답 10초)
- TODO: Stochastic — Pyomo, 솔버, 규모(유닛 × 시간 × 시나리오)와 풀이 시간
- TODO: 수식 기호 ↔ 코드 변수명 대응표

## 12. 참고문헌

- TODO
