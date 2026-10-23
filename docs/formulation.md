# 최적화 모델 정식화 (Formulation)

> 각 절의 담당은 `[C]` 최적화 코어, `[D]` 확률론적 최적화.
> 1~4, 6, 7, 11 절은 구현과 함께 채워져 있다. 5(ESS), 8~10 절은 작성 중이다.
> **수식과 코드가 어긋나면 코드가 맞다** — 11절에 기호 ↔ 코드 대응표가 있다.

## 0. 개요

- 풀려는 문제: 24h(1시간 단위) Unit Commitment + Economic Dispatch
- 접근 3종과 관계: Rule-based(베이스라인) → 결정론적 MILP → Two-Stage Stochastic
- API 매핑: `/dispatch/milp`, `/dispatch/stochastic` ([api_spec.md](api_spec.md))

## 1. 표기법 `[C][D]`

### 1.1 집합

| 기호 | 뜻 |
|---|---|
| $g \in G$ | 급전 가능 발전 유닛 (`config/generators.yaml`, 현재 25기) |
| $t \in T = \{0, \dots, H-1\}$ | 시간. step 1시간, 기본 $H = 24$ |
| $G^{\text{hydro}} \subset G$ | 수력 유닛 (저수율 제약 대상) |
| $s \in S$ | 시나리오 (8절, `[D]`) |

태양광·풍력·기타 신재생은 집합 $G$ 에 **들어가지 않는다**. 비급전(must-take)이라
순수요에서 차감된다 (2.3).

### 1.2 파라미터

| 기호 | 단위 | yaml 필드 | 뜻 |
|---|---|---|---|
| $\underline P_g,\ \overline P_g$ | MW | `p_min_mw`, `p_max_mw` | 가동 시 출력 하한·상한 |
| $c_g$ | 원/MWh | (fuel_cost 조인) | 연료비 단가 |
| $C^{su}_g$ | 원/회 | `startup_cost_krw` | 기동비 |
| $RU_g,\ RD_g$ | MW/h | `ramp_up_mw_per_h`, `ramp_down_mw_per_h` | 램프 한계 |
| $TU_g,\ TD_g$ | h | `min_up_h`, `min_down_h` | 최소 기동·정지 시간 |
| $e_g$ | tCO2/MWh | `co2_ton_per_mwh` | 배출계수 |
| $u^0_g,\ \tau^0_g$ | -, h | `u_init`, `init_state_h` | 초기 가동 상태와 그 지속 시간 |
| $D_t$ | MW | (입력) | 순수요 |
| $r$ | - | `reserve_margin_ratio` | 예비율 요건 (0.10) |
| $\rho$ | % | (dam_status) | 저수율 |
| $\kappa(\rho)$ | - | `hydro.*` | 수력 상한 계수 (4.2-6) |
| $\pi^{shed}$ | 원/MWh | `shed_penalty_krw_per_mwh` | 부하차단 페널티 (옵션) |

### 1.3 결정변수

| 기호 | 범위 | 뜻 |
|---|---|---|
| $u_{g,t}$ | $\{0,1\}$ | 가동 여부 |
| $v_{g,t}$ | $[0,1]$ | 기동 이벤트 |
| $w_{g,t}$ | $[0,1]$ | 정지 이벤트 |
| $p_{g,t}$ | $[0, \kappa\overline P_g]$ | 출력 (MW). step 1시간이라 MWh 와 수치가 같다 |
| $\sigma_t$ | $[0, D_t]$ | 부하차단 슬랙 (옵션, 기본 상한 0) |
| $ch_t,\ dis_t$ | $[0, \overline{CH}], [0, \overline{DIS}]$ | ESS 충전·방전 출력 (MW, 5장) |
| $soc_t$ | $[\underline S, \overline S]$ | ESS 저장 에너지 (MWh, 5장) |

$v, w$ 를 이진변수로 두지 않는다. $u$ 가 이진이고 연결식 (4.2-7) 이 있으므로
기동비 $\ge 0$ 을 최소화하는 해에서 $v_{g,t} = \max(0,\ u_{g,t} - u_{g,t-1})$ 로
결정된다. 이진변수를 2/3 줄여 10초 KPI 에 유리하다. 단 $C^{su}_g = 0$ 인 유닛
(수력)에서는 $v = w = 0.5$ 같은 무차별 해가 가능하므로, **보고용 기동/정지
플래그는 $u$ 에서 다시 계산한다** (`schedule.startup_flags`).

## 2. 파라미터 출처와 유닛 모델링 `[C]`

### 2.1 유닛 블록 (총 25기)

| 발전원 | 블록 수 | $\overline P_g$ (MW) | $\underline P_g$ | $TU/TD$ (h) | 비고 |
|---|---:|---:|---:|---:|---|
| 원자력 | 4 | 8,500 | 55% | 24 / 24 | 기저부하, 램프 10%/h |
| 유연탄 | 6 | 5,000 | 40% | 6 / 6 | 중간부하, 램프 60%/h |
| 무연탄 | 1 | 400 | 40% | 6 / 6 | |
| LNG | 10 | 2,000 | 40% | 2 / 2 | 첨두, 램프 100%/h |
| 유류 | 2 | 180 | 20% | 1 / 1 | merit order 최후순위 |
| 수력 | 2 | 525 | 0 | 1 / 1 | 저수율 제약 대상 |

합계 85,570 MW. CLAUDE.local.md 권장 범위(대표유닛 20~40기)에 들어간다.
블록 수는 `[미정]` 항목이며, 로더와 MILP 는 유닛 수에 무관하게 동작한다.

### 2.2 값의 출처와 가정

| 파라미터 | 출처 | 상태 |
|---|---|---|
| $c_g$ 연료비 | data_api `fuel_cost` (월별, 원/kWh → 원/MWh 는 로더에서 1회) | 실측 |
| $\overline P_g$ | `trading_by_fuel` 실측 최대 x 1.174 (공급능력과 정합) | 가정 |
| $\underline P_g$ | 실측 최소 발전량과 정합하도록 역산한 기술별 비율 | 가정 |
| $e_g$ | IAEA CCNP 2022 (전과정, gCO2eq/kWh x 0.001) 중간값 | 출처 있음 |
| $e_g$ (유류·무연탄) | 해당 항목 없음 — **출처 없는 값** | `[미정]` |
| $RU_g, RD_g$ | 기술별 유연성 순서만 반영 | `[미정]` |
| $TU_g, TD_g$ | 과제 예시(석탄 6h, LNG 2h) + 가정 | `[미정]` |
| $C^{su}_g$ | 기술별 상대 크기만 반영한 placeholder | `[미정]` |
| $u^0_g, \tau^0_g$ | 기저 가동 / 첨두 정지 기본안 | `[미정]` |

근거는 `config/generators.yaml` 의 `meta` 블록에 항목별로 적어 두었다.
$C^{su}_g$ 는 목적함수 계수이므로 절감률 KPI 에 직접 영향을 준다 (민감도 분석 대상).

### 2.3 순수요

$$D_t = \text{demand}_t - (\text{solar}_t + \text{wind}_t + \text{renewable}_t)$$

양수(pumped)는 차감하지 않는다 (ESS 성격이라 별도 모델링). 1년치(4,416h)로
`power_demand` 가 태양광을 포함함을 확인했다 (`data_loader.build_net_demand` 주석).
5분 원본은 시간평균으로 집계하고, 샘플이 90% 미만인 시간은 버린다.

## 3. Rule-based 베이스라인 `[C]`

구현: `services/dispatch_api/baseline.py`. MILP 와 **같은 입력·같은 제약·같은 비용
회계**를 쓰고, 같은 타입(`DispatchResult`)을 돌려준다. 다른 것은 "시야"뿐이다.

### 3.1 규칙

| | 규칙 |
|---|---|
| R1 | 우선순위 = 연료비 오름차순, 동률이면 큰 유닛 먼저, 그다음 id 순 |
| R2 | 매 시간 독립 결정. 다음 시간을 보지 않는다 (no lookahead) |
| R3 | 기동: 그 시간의 $(1+r)D_t$ 를 채울 때까지 우선순위대로 투입 |
| R4 | 정지: ⓐ 더 싼 유닛들의 용량만으로 $D_t$ 가 충족되고 ⓑ 빼도 예비율이 유지되고 ⓒ 램프다운이 허용하면, 비싼 유닛부터 뺀다 |
| R5 | 급전: 투입된 유닛을 우선순위대로 상한까지 채운다 |
| R6 | ESS 없음, 예측 사용 없음 |

R4 의 ⓐ 가 없으면 "가장 싼 유닛을 세우고 비싼 유닛을 돌리는" 급전이 나와
baseline 이 부당하게 나빠진다. 일부러 약한 baseline 을 만들지 않기 위한 조건이다.

### 3.2 규칙의 한계 — 과잉발전

R2(lookahead 없음) 때문에, 직전 시간에 미리 출력을 낮춰 두지 못해 램프다운
한계에 걸리는 경우가 있다. 이때 **제약을 깨지 않고** 과잉분을 `surplus_mw` 에
기록한다. 균형식에서는 $\sum_g p_{g,t} + \sigma_t - \text{surplus}_t = D_t$ 로 본다.
MILP 는 등식 제약이라 항상 0 이다.

### 3.3 절감률 정의식

$$\text{cost reduction} = \frac{C^{\text{rule}} - C^{\text{milp}}}{C^{\text{rule}}} \times 100 \;(\%)$$

$C$ 는 양쪽 모두 연료비 + 기동비 (+ 쓰는 경우 부하차단 페널티)다. 비교 시
baseline 의 페널티는 0 으로 둔다 — 페널티 크기가 절감률을 좌우하면 안 된다.
측정은 `scripts/measure_dispatch_kpi.py` 가 여러 창에서 반복 수행한다.

## 4. 결정론적 UC/ED MILP `[C]`

구현: `services/dispatch_api/milp.py` (PuLP + HiGHS).

### 4.1 목적함수

$$\min \;\; \underbrace{\sum_{g \in G}\sum_{t \in T} c_g\, p_{g,t}}_{\text{연료비}}
\;+\; \underbrace{\sum_{g \in G}\sum_{t \in T} C^{su}_g\, v_{g,t}}_{\text{기동비}}
\;+\; \underbrace{\sum_{t \in T} \pi^{shed}\, \sigma_t}_{\text{부하차단 페널티 (옵션)}}$$

앞의 두 항이 과제 `[필수]` 이고, 세 번째는 feasibility 처리용 **모델링 선택**이다
(4.3). 비용은 solver 의 목적함수 값을 그대로 쓰지 않고 해에서 다시 계산한다
(`schedule.assemble`). baseline 과 같은 코드로 집계해야 비교가 성립하고,
목적함수 값과 비교하면 계수 누락을 잡는 교차검증이 된다.

### 4.2 제약조건

**1. 수급 균형** — 모든 $t$

$$\sum_{g \in G} p_{g,t} + \sigma_t = D_t$$

**2. 예비율** `[제안 — 팀 확정 필요]` — 모든 $t$

$$\sum_{g \in G} \kappa_g \overline P_g\, u_{g,t} \;\ge\; (1 + r)\,(D_t - \sigma_t)$$

"가동 중인 유닛의 상한 합"을 공급능력으로 본다. 정지한 유닛은 그 시간에 즉시 쓸
수 없으므로 제외한다. 부하차단을 쓰는 경우 **차단 후 실제 공급한 수요**를 기준으로
요구한다 (위기 시나리오에서 "차단하고 나면 안정"을 판정하기 위해).

**3. 출력 상·하한** — 모든 $g, t$

$$\underline P_g\, u_{g,t} \;\le\; p_{g,t} \;\le\; \kappa_g \overline P_g\, u_{g,t}$$

**4. 램프율** — 모든 $g$, $t \ge 1$ (그리고 $p^0_g$ 를 아는 경우 $t = 0$)

$$p_{g,t} - p_{g,t-1} \le RU_g\, u_{g,t-1} + \overline{SU}_g\, v_{g,t}$$
$$p_{g,t-1} - p_{g,t} \le RD_g\, u_{g,t} + \overline{SD}_g\, w_{g,t}$$

기동·정지 시점의 한계는
$\overline{SU}_g = \min(\max(\underline P_g, RU_g),\ \kappa_g\overline P_g)$,
$\overline{SD}_g = \min(\max(\underline P_g, RD_g),\ \kappa_g\overline P_g)$ 다.
$\underline P_g$ 를 포함하는 이유는 유닛이 켜지는 순간 최소출력 이상이어야 하므로
($RU_g < \underline P_g$ 이면 영원히 켤 수 없게 된다), $RU_g$ 를 포함하는 이유는
$\underline P_g = 0$ 인 수력이 기동한 시간에 0 밖에 못 내는 상황을 막기 위해서다.

$t = 0$ 의 초기 출력 $p^0_g$: 수집 데이터에 유닛별 실측 출력이 없다 (발전원별
합계뿐). 그래서 요청에 `p_init_mw` 가 없으면 **$t=0$ 의 램프 제약을 걸지 않고**
그 사실을 응답 `notes` 에 남긴다. 모르는 값을 $\underline P_g$ 로 가정하면 첫 시간이
인위적으로 묶여 없는 infeasible 이 생긴다. baseline 도 같은 규칙을 쓴다.

**5·6. 최소 기동/정지 시간** (Rajan–Takriti) — 모든 $g$

$$\sum_{k = t - TU_g + 1}^{t} v_{g,k} \le u_{g,t} \quad (t \ge TU_g - 1)$$
$$\sum_{k = t - TD_g + 1}^{t} w_{g,k} \le 1 - u_{g,t} \quad (t \ge TD_g - 1)$$

초기 상태는 창 밖의 이벤트를 쓸 수 없으므로 상태 고정으로 처리한다.

$$u^0_g = 1,\ \tau^0_g < TU_g \;\Rightarrow\; u_{g,t} = 1,\ t < TU_g - \tau^0_g$$
$$u^0_g = 0,\ \tau^0_g < TD_g \;\Rightarrow\; u_{g,t} = 0,\ t < TD_g - \tau^0_g$$

**7. 기동/정지 연결식** — 모든 $g, t$ (단 $u_{g,-1} = u^0_g$)

$$u_{g,t} - u_{g,t-1} = v_{g,t} - w_{g,t}$$

**6'. 수력 저수율 제한** `[필수 — 강도는 미정]`

상한 계수 $\kappa_g$ 는 수력에만 적용된다 ($g \notin G^{\text{hydro}}$ 이면 1).
임계값 $\rho_0 = 30\%$ 는 과제가 정한 값이고, 제한 강도는 제안이다
(`config/settings.yaml` 의 `hydro`):

$$\kappa_g(\rho) = \begin{cases}
1 & \rho \ge \rho_0 \;\text{또는}\; \rho \;\text{미지} \\
\kappa_{\min} + (1 - \kappa_{\min})\dfrac{\rho}{\rho_0} & \rho < \rho_0,\ \text{linear (기본)} \\
\kappa_{\min} & \rho < \rho_0,\ \text{fixed}
\end{cases}$$

기본값 $\kappa_{\min} = 0.5$. linear 를 기본으로 둔 이유는 30.0% 와 29.9% 사이에서
급전 계획이 계단처럼 바뀌지 않게 하려는 것이다 (해의 안정성). 저수율은 여러 댐의
최신 관측 평균을 쓴다 (현재 수집된 댐은 소양강 1기).

### 4.3 부하차단 슬랙과 페널티

$\sigma_t$ 는 **기본적으로 상한 0 으로 묶여 있다** (`allow_load_shedding: false`).
과제 필수 제약이 아니라 feasibility 처리용 모델링 선택이기 때문이다. 켜면

- 용량 부족이 infeasible 대신 "부족량 MWh" 로 나온다 (위기 시나리오 진단)
- 응답의 `hours[].shed_mw`, `cost.shed_won`, `notes` 에 사용 사실이 드러난다
- 정상 해와 구분된다 (제약 검증 리포트의 "부하차단 미사용" 항목이 실패로 표시)

$\pi^{shed} = 10^7$ 원/MWh (= 10,000 원/kWh) 는 가장 비싼 연료비(유류 279 원/kWh)의
수십 배다. 즉 "돌릴 수 있는 발전기가 하나라도 있으면 반드시 돌린다"가 보장된다.
VOLL 추정치로 쓸 수 있으나 국내 공식값 근거는 `[미정]` 이다.

### 4.4 해의 사후 검증

solver 가 optimal 을 돌려주는 것과 모델이 맞는 것은 다르다. 그래서 해를 받은 뒤
제약을 **독립적으로 다시 검사**한다 (`services/dispatch_api/validation.py`).
수급 균형·상하한·예비율·램프업·램프다운·최소 기동/정지·수력 제한·과잉발전·
부하차단, 그리고 ESS 를 쓰면 ESS 5종(출력 한계·SOC 범위·SOC 전이식·종료 SOC·
동시 충방전)까지 검사해 응답 `constraints` 로 돌려준다. SOC 전이식은 효율까지
포함해 **다시 계산해서** 비교한다 — 효율을 빼먹는 실수는 비용이 그럴듯하게
나오기 때문에 결과만 보면 잡히지 않는다. 허용오차는 1 MW
(7만 MW 급에서 상대오차 $1.4\times10^{-5}$). baseline 도 같은 검사를 통과해야
비교가 공정하다.

## 5. ESS 연계 `[C]`

구현: `milp.py` 의 제약 8 (후처리가 아니라 **같은 MILP 안의 결정변수**).
사양은 `config/ess.yaml`.

### 5.1 변수와 파라미터

| 기호 | 단위 | 뜻 |
|---|---|---|
| $ch_t,\ dis_t$ | MW | 충전·방전 출력 |
| $soc_t$ | MWh | 저장 에너지 |
| $z_t \in \{0,1\}$ | - | 동시 충·방전 금지용 (옵션) |
| $\overline{CH},\ \overline{DIS}$ | MW | 충전·방전 출력 한계 |
| $E$ | MWh | 에너지 용량 |
| $\eta$ | - | 왕복효율. $\eta_{ch} = \eta_{dis} = \sqrt{\eta}$ |
| $\underline{S},\ \overline{S}$ | MWh | SOC 하한·상한 (용량의 %로 입력) |
| $S^0,\ S^{end}$ | MWh | 초기 SOC, 종료 SOC 하한 |

SOC 는 **MWh 로 다룬다**. 비율로 다루면 "SOC 0.5" 가 50%인지 0.5 MWh인지
구분되지 않고, 전이식에서 용량 곱을 빼먹는 버그가 생긴다. yaml 은 사람이 읽기
쉬운 %로 받고 로더가 MWh 로 환산한다 (`EssSpec.soc_*_mwh`).

### 5.2 제약

**수급 균형 (1번 제약 갱신)** — 방전은 공급, 충전은 부하다.

$$\sum_{g \in G} p_{g,t} + \sigma_t + (dis_t - ch_t) = D_t$$

**예비율 (2번 제약 갱신)** — 분모는 발전기가 담당하는 부하다.

$$\sum_{g} \kappa_g \overline P_g u_{g,t} \;\ge\; (1+r)\,\big(D_t - \sigma_t - (dis_t - ch_t)\big)$$

ESS 의 남은 방전 여력을 예비력으로 셀지는 `[미정]` 이라 세지 않는다 (보수적).

**SOC 전이식** — $\Delta t = 1$h

$$soc_t = soc_{t-1} + \eta_{ch}\, ch_t\, \Delta t - \frac{dis_t\, \Delta t}{\eta_{dis}},
\qquad soc_{-1} = S^0$$

**SOC 범위와 출력 한계**

$$\underline S \le soc_t \le \overline S, \qquad
0 \le ch_t \le \overline{CH}, \qquad 0 \le dis_t \le \overline{DIS}$$

**종료 SOC**

$$soc_{H-1} \ge S^{end}$$

이 제약이 **없으면 안 된다**. 없으면 MILP 가 하루 동안 배터리를 비우고 끝내
"공짜 에너지"를 얻고, 그만큼 비용이 부당하게 낮아진다. 기본값은 $S^{end} = S^0$
(50%)로, 하루 단위 비교가 성립하게 했다.

**동시 충·방전 금지** (옵션, `enforce_exclusive`)

$$ch_t \le \overline{CH}\, z_t, \qquad dis_t \le \overline{DIS}\,(1 - z_t)$$

기본은 **끄는 것**이다. $\eta < 1$ 이고 음수 가격이 없으면 동시 충·방전은 항상
손실이라 비용 최소화 해에서 저절로 배제된다 (`tests/test_ess.py` 가 확인한다).
켜면 시간당 이진변수가 1개 늘어난다.

### 5.3 운전 목적 `[모델링 선택]`

| mode | 목적함수/제약 | 쓰는 곳 |
|---|---|---|
| `cost` (기본) | 4.1 그대로. ESS 는 비용 차익거래로만 움직인다 | baseline 과의 비용 비교 |
| `peak_shaving` | 4.1 + 하드 제약 $peak \le (1 - \tau)\max_t D_t$ | 피크 감소율 5% KPI 판정 |
| `combined` | 4.1 + $\lambda \cdot peak$ 가중합 | 비용과 피크의 절충 탐색 |

여기서 $peak \ge D_t - \sigma_t - (dis_t - ch_t)\ \forall t$ 로 선형화한다.

**cost 모드는 피크 감소를 보장하지 않는다.** 단가가 시간에 따라 다르지 않으면
ESS 를 쓸 이유가 없어 감소율이 0 이 된다. 그래서 KPI 는 `peak_shaving` 으로
측정하고, 목표가 물리적으로 불가능하면 infeasible 로 드러나게 둔다
(성공 응답을 억지로 만들지 않는다).

`combined` 의 $\lambda$ 는 비용이 아니라 "피크 1 MW 를 얼마로 볼지"의 선호도다.
그래서 이 모드의 `total_cost` 는 baseline 과의 비용 비교에 그대로 쓰면 안 되고,
목적함수-비용 교차검증에서도 제외한다.

### 5.4 피크 감소율 정의식

$$\text{peak reduction} = \frac{P^{before} - P^{after}}{P^{before}} \times 100\;(\%)$$

- $P^{before} = \max_t D_t$ — ESS 미적용 순수요 피크. ESS 가 없으면 발전기가
  순수요를 그대로 담당하므로 이것이 곧 피크 부하다
- $P^{after} = \max_t \big(D_t - \sigma_t - (dis_t - ch_t)\big)$ — ESS 적용 후 발전기 부하 피크
- 기준: $\ge 5\%$

### 5.5 규모와 효율 `[미정 — 근거 있는 제안]`

| 항목 | 값 | 근거 |
|---|---:|---|
| 충전·방전 출력 | 4,700 MW | `trading_by_fuel.capacity_mw` 의 pumped(양수) 설비. 1년 내내 일정 |
| 에너지 용량 | 32,900 MWh | 4,700 MW x 7h. 아래 검산으로 정함 |
| 왕복효율 | 0.75 | 양수발전 전형값 0.70~0.80 의 중간 |
| SOC | 10 / 50 / 95 % | 하한 / 초기·종료 / 상한 |

양수발전을 ESS 로 모델링한 것이다 (`generators.yaml` 은 양수를 제외한다 —
펌핑 중 음수 출력이라 성격이 ESS와 같다). 출력은 출처 있는 값이고, 지속시간
7h 는 다음 검산으로 정했다.

1년치 253개 24h 창에서 피크를 5% 깎는 데 필요한 양:

| | 중앙 | P90 | 최대 |
|---|---:|---:|---:|
| 출력 | 3,546 MW | 4,215 MW | 4,584 MW |
| 에너지 | 11,167 MWh | 15,868 MWh | 27,171 MWh |

출력 4,700 MW 는 전 창을 커버한다. 에너지 27,171 MWh 를 사용 가능 구간
(SOC 10~95% = 용량의 85%)에 담으려면 약 32,000 MWh 가 필요해서 32,900 MWh
(7h)로 잡았다. 6h(28,200 MWh)로 하면 253창 중 4창에서 에너지가 모자랐다.

왕복효율 0.85 는 리튬이온 배터리 기준 예시이고 과제 고정값이 아니다. 양수를
모델링하므로 배터리 값을 쓰면 효율을 과대평가한다.

### 5.6 KPI 측정 방법 — 창 정렬에 주의

피크 감소율은 **자정 정렬 24시간 창**에서 측정한다
(`scripts/measure_dispatch_kpi.py --align-hour 0`).

이유: ESS 의 SOC 정책(초기 = 종료 = 50%)은 하루 단위로 정의돼 있고, day-ahead
급전 계획의 horizon 도 달력상 하루다. 정렬하지 않고 24시간씩 잘라 측정하면
16시에 시작하는 창이 생기고, 그 창에서는 그날 저녁 피크가 horizon 의 1~2번째
시간에 온다. 피크 전에 충전할 시간이 없으니 목표를 못 맞추는데, 이건 ESS 의
한계가 아니라 창을 자른 방식의 문제다.

실측으로 확인한 차이 (1년, `peak_shaving` 모드, 목표 5%):

| 창 정렬 | 해가 있는 창 | 피크 감소율 평균 |
|---|---:|---:|
| 정렬 없음 (24h 간격으로 뒤로) | 213 / 253 | 5.25% |
| 자정 정렬 (day-ahead) | 아래 5.7 참고 | - |

정렬하지 않은 창에서 infeasible 40건은 7~9월에 30건, 창 시작이 14~16시인 경우에
31건 몰려 있었다. 반면 연중 피크가 가장 높은 날(2025-08-25, 순수요 피크
91,686 MW, cap 초과 에너지 14,753 MWh)은 자정 정렬에서는 현재 ESS 규모로
5% 를 달성한다. 출력·용량·초기 SOC 를 늘려도 결과가 같아서(모두 5.00%),
그 날의 제약은 ESS 규모가 아니라 목표 자체였음을 확인했다.

### 5.7 비용·탄소에 주는 영향 (측정)

- ESS 를 켜면 비용이 내려간다 (싼 시간 충전 → 비싼 시간 방전)
- **탄소는 늘어날 수 있다.** 왕복효율 손실만큼 추가 발전이 필요하고, 그 추가
  발전을 담당하는 건 가장 싼 발전원(석탄)이다. 비용-탄소 trade-off 가 ESS 에도
  그대로 나타난다 (`carbon.compare` 로 전/후 비교)
- baseline 은 ESS 를 쓰지 않는다(R6). 따라서 "ESS 포함 MILP vs ESS 없는
  baseline" 비용 비교는 조건이 다르므로, 응답 `notes` 에 그 사실을 밝힌다.
  공정성 판단은 `[미정]` 이다 (CLAUDE.md: baseline 에 ESS 포함 여부는 팀 확정)

## 6. 탄소 배출 `[C]`

구현: `services/dispatch_api/carbon.py`. 급전 스케줄을 입력으로 받는 독립 모듈이라
MILP·baseline·(추후) ESS 전후에 같은 함수를 쓴다.

$$E_t = \sum_{g \in G} e_g\, p_{g,t}\, \Delta t \quad (\text{tCO2}), \qquad
E^{\text{total}} = \sum_{t \in T} E_t$$

$\Delta t = 1$h 이므로 $p_{g,t}$(MW)가 곧 발전량(MWh)이다. step 을 바꾸면
`step_h` 로 넘겨야 한다. 집계는 시간대별·발전원별·유닛별·기간 총계를 제공하고,
배출집약도 $E^{\text{total}} / \sum_{g,t} p_{g,t}$ (tCO2/MWh) 도 함께 낸다.

배출계수 출처는 2.2 표 참고 (유류·무연탄은 출처 없는 값 — `[미정]`).

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

> 상태: **미구현**. 변형 규칙(수요 x1.15 / 용량 -1,400 MW / 태양광 x0.5)을 적용할
> 지점은 이미 준비돼 있다 — 요청의 `net_demand_mw`, `unit_ids`, `reserve_ratio`,
> `allow_load_shedding` 으로 같은 MILP 코어를 재실행하면 된다. 남은 일은
> 시나리오 러너와 예비율 5% 판정, 전용 endpoint 다.

- TODO: 폭염(수요 +15%), 발전소 탈락(1,400MW), 태양광 50% 램프다운 — 각각 파라미터 변경 방식
- TODO: 예비율 5% 유지 판정

## 11. 구현 매핑 `[C][D]`

### 11.1 모듈

| 파일 | 역할 |
|---|---|
| `loaders/config_loader.py` | yaml → 타입 객체, 연료비 결합, 원/kWh → 원/MWh (유일 지점) |
| `loaders/data_loader.py` | CSV/REST → 1시간 집계 → 순수요 → `DispatchInput` |
| `models.py` | 도메인 타입 (`Generator`, `DispatchInput`, `DispatchResult`, `HydroPolicy`) |
| `milp.py` | 4장의 UC + ED MILP + 5장의 ESS (PuLP) |
| `baseline.py` | 3장의 우선순위법 |
| `schedule.py` | 결과 조립과 비용 회계 (MILP·baseline 공용), 절감률, merit order |
| `validation.py` | 4.4 의 제약 사후 검증 |
| `carbon.py` | 6장의 배출 계산 |
| `scenarios.py` | 7장의 시나리오 생성 `[D]` |
| `service.py` | 유스케이스 조립 (route 는 검증·호출·직렬화만) |
| `scripts/measure_dispatch_kpi.py` | 절감률·응답시간·ESS 피크 감소율·제약 위반 반복 측정 |

### 11.2 기호 ↔ 코드

| 수식 | 코드 |
|---|---|
| $u_{g,t}$ | `u[g.id][t]` (Binary) |
| $v_{g,t},\ w_{g,t}$ | `v[g.id][t]`, `w[g.id][t]` (Continuous [0,1]) |
| $p_{g,t}$ | `p[g.id][t]` |
| $\sigma_t$ | `shed[t]` |
| $D_t$ | `inp.net_demand_mw[t]` |
| $r$ | `inp.reserve_margin_ratio` |
| $\kappa_g \overline P_g$ | `p_max_eff[g.id]` (`schedule.effective_p_max`) |
| $\overline{SU}_g,\ \overline{SD}_g$ | `schedule.startup_ramp_mw`, `shutdown_ramp_mw` |
| $ch_t,\ dis_t,\ soc_t$ | `charge[t]`, `discharge[t]`, `soc[t]` |
| $peak$ | `peak_var` |
| 제약 이름 | `balance_t`, `reserve_t`, `pmin/pmax_g_t`, `rampup/rampdown_g_t`, `minup/mindown_g_t`, `link_g_t`, `init_on/init_off_g_t`, `soc_t`, `soc_final`, `peak_def_t`, `peak_cap`, `ess_ch/dis_excl_t` |

### 11.3 solver 와 문제 크기

- PuLP + **HiGHS** (기본). CBC 는 대안 — PuLP 번들 CBC 바이너리는 Apple Silicon
  에서 실행되지 않는다 (`settings.yaml` 주석).
- `solver_time_limit_s`, `solver_mip_gap` 은 `config/settings.yaml`, 요청별로는
  `solver.time_limit_s` / `solver.mip_gap` 으로 덮어쓴다.
- 29기 x 24h 기준 실측: 변수 2,808개 (이진 696개 = $u$ 만), 제약 4,564개,
  모델 빌드 0.03s, solve 0.09~0.56s. 10초 KPI 대비 여유가 크다.
  ESS 를 켜면 연속변수 3H(+ peak 모드는 1)개와 제약 H+1 개가 늘고, 이진변수는
  `enforce_exclusive` 를 켤 때만 H 개 늘어난다. 측정상 solve 시간은 오히려
  줄기도 한다 (ESS 가 LP 완화를 느슨하게 만들어 분기가 줄어든다).
- 매 실행마다 변수·이진변수·제약 수와 빌드/풀이 시간을 로그와 응답
  (`model_size`)에 남긴다.

## 12. 참고문헌

- TODO
