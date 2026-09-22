# 데이터 소스 정리

## 노션 페이지
 노션 -> 코디세이 팀플 -> 코디세이 개발 -> 1주차 이권희

## 공통

- 과거 데이터 기간: **2025-07-01 ~ 2026-08-01**
- CSV 파일 경로: `data/csv` (UTF-8 변환본만 사용)

키는 프로젝트 루트 `.env`에 둔다 (git 제외). 아래 URL의 `${...}`는 해당 환경변수 값으로 치환한다.

| 서비스 | 인증 파라미터 | 환경변수 (`.env`) |
|---|---|---|
| 공공데이터포털 | `serviceKey` | `DATA_GO_KR_SERVICE_KEY` |
| 기상청 API허브 | `authKey` | `KMA_AUTH_KEY` |

---

## 전력 수요

### 과거_전력_수요_utf8_2025.07.01~2026.08.01.csv

- 구분: 과거
- 경로: `data/csv/과거_전력_수요_utf8_2025.07.01~2026.08.01.csv`
- 형태: 5분 단위, 113,734행, 시간 오름차순, 빈 값 없음
- 결측: 연속 5분 간격이 끊기는 구간 570곳 (예: `20250701235000` 다음이 `20250702000000`) — 기대 행 수 114,336 대비 602행 부족

| 컬럼 | 설명 | 예시 |
|---|---|---|
| 기준일시 | YYYYMMDDHHmmss | 20250701000000 |
| 공급능력(MW) | | 98790.0 |
| 현재수요(MW) | | 67513.3 |
| 최대예측수요(MW) | | 86600.0 |
| 공급예비력(MW) | | 31276.7 |
| 공급예비율(%) | | 46.3267 |
| 운영예비력(MW) | | 13096.9 |
| 운영예비율(%) | | 19.399 |

> 실시간 API `getSukub5mMaxDatetime2`와 같은 항목 (currPwrTot = 현재수요 등)

### 한국전력거래소_현재전력수급현황조회_GW

- 구분: 실시간
- 용도: 매 시간 전력 수요 값 확인
- 문서: [data.go.kr/15158704](https://www.data.go.kr/data/15158704/openapi.do#/API%20%EB%AA%A9%EB%A1%9D/getSukub5mMaxDatetime2)
- 파라미터: `serviceKey`, `dataType=json`

```
GET https://apis.data.go.kr/B552115/sukub5mMaxDatetime2/getSukub5mMaxDatetime2?serviceKey=${DATA_GO_KR_SERVICE_KEY}&dataType=json
```

응답: `response.body.items.item`

| 필드 | 설명 |
|---|---|
| baseDatetime | 기준일시 |
| currPwrTot | 현재수요 |
| forecastLoad | 최대예측수요 |
| suppAbility | 공급능력 |
| suppReservePwr | 공급예비력 |
| suppReserveRate | 공급예비율 |
| operReservePwr | 운영예비력 |
| operReserveRate | 운영예비율 |
| rn | 순번 |

### EPSIS 실시간 전력수급 (보조)

- 구분: 실시간, **보조 소스** — 주 소스는 위 `현재전력수급현황조회_GW` API
- 형태: 브라우저 조회 (API 아님)
- 링크: https://epsis.kpx.or.kr/epsisnew/selectEkgeEpsMepRealChart.do?menuId=030300

---

## 발전원별 발전량

### 연료원별_utf8_20250701_20260801.csv (연료원별 과거 전력입찰량)

- 구분: 과거
- 경로: `data/csv/연료원별_utf8_20250701_20260801.csv`
- 출처: https://epsis.kpx.or.kr/epsisnew/selectEkmaBddBftChart.do?menuId=040401
- 형태: 월 단위, 13행 (2026/07 → 2025/07 내림차순), 파일 앞에 UTF-8 BOM 있음 (`utf-8-sig`로 읽기)
- 기간 주의: 2026/08 행 없음 (마지막 달이 2026/07)
- 단위: 파일에 표기 없음 (출처 화면 확인 필요)

| 컬럼 | 설명 | 예시 (2026/07) |
|---|---|---|
| 기간 | YYYY/MM | 2026/07 |
| 지역 | 전 행이 `합계` | 합계 |
| 원자력 | | 14662.04 |
| 유연탄 | | 24845.42 |
| 무연탄 | | 270.06 |
| 유류 | | 35.27 |
| LNG | | 30683.51 |
| 양수 | | 4.468 |
| 신재생 | | 0 |
| 석탄가스화 | | 0 |
| 태양 | | 0 |
| 풍력 | | 0.703 |
| 수력 | | 313.29 |
| 해양 | | 0 |
| 바이오 | | 271.75 |
| 폐기물 | | 0 |
| 기타 | | 152.42 |
| 합계 | | 71238.94 |

### 한국전력거래소_발전원별 발전량 현황조회_GW (+풍력)

- 구분: 실시간
- 문서: [data.go.kr/15158491](https://www.data.go.kr/data/15158491/openapi.do?dType=API#/API%20목록/getSumperfuel5m)
- 파라미터: `serviceKey`, `dataType=json`, `numOfRows`, `pageNo`
- 발전량 단위: MW

```
GET https://apis.data.go.kr/B552115/sumperfuel5m/getSumperfuel5m?serviceKey=${DATA_GO_KR_SERVICE_KEY}&dataType=json&numOfRows=5&pageNo=1
```

응답: `response.body.items.item`

| 필드 | 설명 |
|---|---|
| baseDateTime | 기준일시 |
| fuelPwr1 | 수력 |
| fuelPwr2 | 유류 |
| fuelPwr3 | 유연탄 |
| fuelPwr4 | 원자력 |
| fuelPwr5 | 양수 |
| fuelPwr6 | 가스 |
| fuelPwr7 | 국내탄 |
| fuelPwr8 | 태양광(시장) |
| fuelPwr9 | 풍력 |
| fuelPwr10 | 신재생 |
| pEsmw | PPA 추정 |
| bEmsw | BTM 추정 |
| fuelPwrTot | 시장수요(현재) |
| rn | 순번 |

> - 풍력(`fuelPwr9`)은 이 API에만 있음 (다른 API로 대체 불가)
> - `fuelPwr8`, `fuelPwr9`의 의미가 계통기준 API와 다름

### 한국전력거래소_발전원별 발전량(계통기준) (+baseDate)

- 구분: 실시간
- 문서: [data.go.kr/15113384](https://www.data.go.kr/data/15113384/openapi.do#/API%20목록/getPwrAmountByGen)
- 파라미터: `serviceKey`, `pageNo`, `numOfRows`, `dataType=json`, `baseDate` (YYYYMMDD)

```
GET https://apis.data.go.kr/B552115/PwrAmountByGen/getPwrAmountByGen?serviceKey=${DATA_GO_KR_SERVICE_KEY}&pageNo=1&numOfRows=5&dataType=json&baseDate=20260404
```

응답: `response.body.items.item`

| 필드 | 설명 |
|---|---|
| baseDatetime | 기준일시분 |
| fuelPwr1 | 수력 |
| fuelPwr2 | 유류 |
| fuelPwr3 | 유연탄 |
| fuelPwr4 | 원자력 |
| fuelPwr5 | 양수 |
| fuelPwr6 | 가스 |
| fuelPwr7 | 국내탄 |
| fuelPwr8 | 신재생 |
| fuelPwr9 | 태양광 |
| fuelPwrTot | 합계 |
| rn | 순번 |

> - `fuelPwr8`, `fuelPwr9`의 의미가 현황조회_GW API와 다름

---

## 발전원별 단가

### 시간별_전력_거래_가격_utf8_2025.07.01~2026.08.01.csv

- 구분: 과거
- 경로: `data/csv/시간별_전력_거래_가격_utf8_2025.07.01~2026.08.01.csv`
- 형태: 일 단위, 397행 (2026/08/01 → 2025/07/01 내림차순), 빈 값 없음
- 가로형: 한 행에 24개 시간 컬럼 — 시계열로 쓰려면 세로로 펼쳐야 함

| 컬럼 | 설명 | 예시 (2026/08/01) |
|---|---|---|
| 기간 | YYYY/MM/DD | 2026/08/01 |
| 01시 ~ 24시 | 시간별 SMP, 종료 시각 기준 (`01시` → 00:00~01:00) | 121.47, 109.01, … |
| 최대 | 당일 최대 | 174.1 |
| 최소 | 당일 최소 | 102.26 |
| 가중평균 | 당일 가중평균 | 147.75 |

### 연료별_연료비_단가_utf8_2025.07.01~2026.08.01.csv

- 구분: 과거
- 경로: `data/csv/연료별_연료비_단가_utf8_2025.07.01~2026.08.01.csv`
- 형태: 월 단위, 14행 (2026/08 → 2025/07 내림차순)
- **헤더 3줄**: 1행 대분류(연료단가/열량단가/연료비단가, 병합 셀이라 빈칸), 2행 연료명, 3행 단위 — 읽을 때 3줄을 합쳐 컬럼명을 만들어야 함
- 무연탄 연료단가가 0인 달이 있음 (예: 2025/07, 2025/08)

| 대분류 | 연료 | 단위 | 예시 (2026/07) |
|---|---|---|---|
| (기간) | | YYYY/MM | 2026/07 |
| 연료단가 | 원자력 | 원/kWh | 6.498 |
| 연료단가 | 유연탄 | 원/ton | 200068.40 |
| 연료단가 | 무연탄 | 원/ton | 199932.58 |
| 연료단가 | 유류 | 원/kl | 545543.75 |
| 연료단가 | LNG | 원/ton | 1077079.84 |
| 열량단가 | 원자력·유연탄·무연탄·유류·LNG | 원/Gcal | 2612.99, 38845.32, … |
| 연료비단가 | 원자력·유연탄·무연탄·유류·LNG | 원/kWh | 6.498, 87.15, 131.23, 269.47, 141.85 |

### 한국전력거래소_계통한계가격 및 수요예측(하루전 발전계획용)

- 구분: 실시간
- 문서: [data.go.kr/15131225](https://www.data.go.kr/data/15131225/openapi.do#/API%20목록/getSmpWithForecastDemand)
- 파라미터: `serviceKey`, `pageNo`, `numOfRows=48`, `dataType=json`, `date` (YYYYMMDD)

```
GET https://apis.data.go.kr/B552115/SmpWithForecastDemand/getSmpWithForecastDemand?serviceKey=${DATA_GO_KR_SERVICE_KEY}&pageNo=1&numOfRows=48&dataType=json&date=20260910
```

응답: `response.body.items.item` — totalCount 48 (24시 × 2)

| 필드 | 설명 |
|---|---|
| date | 일시 (YYYYMMDD) |
| hour | 시간 (string, `"01"`~`"24"`), 종료 시각 기준 — 예: `"06"` → 05:00~06:00 |
| areaName | 지역 (육지, 제주) |
| smp | 계통한계가격 (number) |
| mlfd | 육지 예측수요 |
| jlfd | 제주 예측수요 |
| slfd | 총 예측수요 |
| rn | 순번 |

> - 반환값의 hour필드 값이 `01`, `1` 방식으로 여러번 표기되며 자료가 중복됨

---

## 기상 (기온, 습도, 일사량, 풍속)

### 기상청 API허브 — kma_sfctm3

- 구분: 과거 / 실시간
- 사이트: https://apihub.kma.go.kr/
- 파라미터: `tm1` / `tm2` (YYYYMMDDHHmm), `stn` (지점번호), `help=0`, `authKey`

```
GET https://apihub.kma.go.kr/api/typ01/url/kma_sfctm3.php?tm1=201512110100&tm2=201512140000&stn=108&help=0&authKey=${KMA_AUTH_KEY}
```

응답 필드 (✅ = 대상 항목)

| 대상 | 필드 | 설명 |
|---|---|---|
| | TM | 관측시각 (KST) |
| | STN | 국내 지점번호 |
| | WD | 풍향 (36방위) |
| ✅ | WS | 풍속 (m/s) |
| | GST_WD | 돌풍향 (36방위) |
| | GST_WS | 돌풍속 (m/s) |
| | GST_TM | 돌풍속이 관측된 시각 (시분) |
| | PA | 현지기압 (hPa) |
| | PS | 해면기압 (hPa) |
| | PT | 기압변화경향 (Code 0200) |
| | PR | 기압변화량 (hPa) |
| ✅ | TA | 기온 (C) |
| | TD | 이슬점온도 (C) |
| ✅ | HM | 상대습도 (%) |
| | PV | 수증기압 (hPa) |
| | RN | 강수량 (mm) |
| | RN_DAY | 위 관측시간까지의 일강수량 (mm) |
| | RN_JUN | 일강수량 (mm) |
| | RN_INT | 강수강도 (mm/h) |
| | SD_HR3 | 3시간 신적설 (cm) |
| | SD_DAY | 일 신적설 (cm) |
| | SD_TOT | 적설 (cm) |
| | WC | GTS 현재일기 (Code 4677) |
| | WP | GTS 과거일기 (Code 4561) |
| | WW | 국내식 일기코드 (문자열 22개) |
| | CA_TOT | 전운량 (1/10) |
| | CA_MID | 중하층운량 (1/10) |
| | CH_MIN | 최저운고 (100m) |
| | CT | 운형 (문자열 8개) |
| | CT_TOP | GTS 상층운형 (Code 0509) |
| | CT_MID | GTS 중층운형 (Code 0515) |
| | CT_LOW | GTS 하층운형 (Code 0513) |
| | VS | 시정 (10m) |
| | SS | 일조 (hr) |
| ✅ | SI | 일사 (MJ/m2) |
| | ST_GD | 지면상태 코드 (관측정책과 문의) |
| | TS | 지면온도 (C) |
| | TE_005 | 5cm 지중온도 (C) |
| | TE_01 | 10cm 지중온도 (C) |
| | TE_02 | 20cm 지중온도 (C) |
| | TE_03 | 30cm 지중온도 (C) |
| | ST_SEA | 해면상태 코드 (관측정책과 문의) |
| | WH | 파고 (m) |
| | BF | Beaufort 최대풍력 (GTS코드) |
| | IR | 1(강수포함, 자동관측), 3(무강수 또는 결측), 4(강수포함, 수동입력) |
| | IX | 유인관측/무인관측 |

### 지역 기준

1. 전력 생산량이 가장 큰 지역 기준 (예: 풍력 → 대관령)
2. 전국 수치 평균 기준

---

## 댐 수위

### 한국수자원공사_수문 운영 정보

- 구분: 과거 / 실시간
- 문서: https://data.go.kr/data/15099110/openapi.do
- 파라미터: `damcode`, `stdt` / `eddt` (YYYY-MM-DD), `pageNo`, `numOfRows`, `_type`, `serviceKey`

```
GET https://apis.data.go.kr/B500001/dam/sluicePresentCondition/mntlist?damcode=2022510&stdt=2018-10-01&eddt=2018-10-01&pageNo=1&numOfRows=10&_type=xml&serviceKey=${DATA_GO_KR_SERVICE_KEY}
```

응답: `response.body.items.item`

| 필드 | 설명 |
|---|---|
| obsrdtmnt | 관측일시 |
| lowlevel | 댐수위 (EL.m) |
| inflowqy | 유입량 (㎥/sec) |
| totdcwtrqy | 총방류량 (㎥/sec) |
| rsvwtqy | 저수량 (백만㎥) |
| rsvwtrt | 저수율 (%) |
| rf | 강우량 (㎜) |

### 댐 코드

| 댐 | 코드 |
|---|---|
| 감포 | 2403201 |
| 강정고령보 | 2011602 |
| 강천보 | 1007601 |
| 공주보 | 3012601 |
| 광동 | 1001210 |
| 구미보 | 2009602 |
| 구천 | 2503220 |
| 군남 | 1021701 |
| 군위 | 2008101 |
| 낙단보 | 2009601 |
| 낙동강하굿둑 | 2022510 |
| 남강 | 2018110 |
| 달방 | 1302210 |
| 달성보 | 2014601 |
| 대곡 | 2201231 |
| 대암 | 2201230 |
| 대청 | 3008110 |
| 대청조정지 | 3008611 |
| 밀양 | 2021110 |
| 백제보 | 3012602 |
| 보령 | 3203110 |
| 보현산 | 2012101 |
| 부안 | 3303110 |
| 사연 | 2201220 |
| 상주보 | 2007601 |
| 선암 | 2301210 |
| 섬진강 | 4001110 |
| 성덕 | 2002111 |
| 세종보 | 3010601 |
| 소양강 | 1012110 |
| 수어 | 4105210 |
| 승촌보 | 5004601 |
| 안계 | 2101210 |
| 안동 | 2001110 |
| 안동조정지 | 2001611 |
| 여주보 | 1007602 |
| 연초 | 2503210 |
| 영주 | 2004101 |
| 영천 | 2012210 |
| 용담 | 3001110 |
| 운문 | 2021210 |
| 이포보 | 1007603 |
| 임하 | 2002110 |
| 임하조정지 | 2002610 |
| 장흥 | 5101110 |
| 주암(본) | 4007110 |
| 주암(조) | 4104610 |
| 주암역조정지 | 4204612 |
| 죽산보 | 5004602 |
| 창녕함안보 | 2017601 |
| 충주 | 1003110 |
| 충주조정지 | 1003611 |
| 칠곡보 | 2011601 |
| 평림 | 5002201 |
| 평화의댐 | 1009710 |
| 합천 | 2015110 |
| 합천조정지 | 2018611 |
| 합천창녕보 | 2014602 |
| 횡성 | 1006110 |

---

## 발전원별 탄소 배출량

### IAEA 「Climate Change and Nuclear Power 2022」

- 비고: 발전기·발전량에 따라 계속 달라져 수치 확보가 어려움
- 위치: 뷰어 100쪽(인쇄 99쪽), Box 14·그림 50 / 원자료: UNECE 2022
- 링크: https://www.iaea.org/sites/default/files/2025-03/iaea-ccnp2022-body-web.pdf
- 기준: 전과정(Life cycle) 온실가스 배출량, 단위 **g CO₂eq/kWh**

| 발전원 | 최소 | 최대 |
|---|---|---|
| 원자력 | 5.1 | 6.4 |
| 육상풍력 | 7.8 | 16 |
| 해상풍력 | 12 | 23 |
| 수력 | 6.0 | 147 |
| 태양광(PV) | 8.0 | 83 |
| 태양열(CSP) | 27 | 122 |
| 천연가스(탄소포집·저장 CCS 적용) | 92 | 220 |
| 천연가스(복합화력) | 403 | 513 |
| 석탄(탄소포집·저장 CCS 적용) | 147 | 469 |
| 석탄 | 751 | 1,095 |