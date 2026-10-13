2026-10-06
milp part에서 요청한 data를 정리하고 찾은 정보를 정리한 문서
추정치 있음, 찾은 data source들은 모두 ..인 /data/data-source 폴더에 저장됨


[출력]
용어                      영문                       단위       의미
설비용량(정격용량)        Installed/Rated Capacity   MW         발전기의 정격 최대 출력
최대발전용량(최대출력)    Pmax                       MW         운전 시 낼 수 있는 출력 상한
최소발전용량(최소출력)    Pmin                       MW         안정 운전이 가능한 출력 하한
출력증가율 		  Ramp Up Rate		     MW      
출력감소율 		  Ramp Down Rate	     MW
[시간]
용어                      영문                       단위       의미
최소운전시간              Minimum Up Time            h          기동 후 최소한 유지해야 하는 운전 시간
최소정지시간              Minimum Down Time          h          정지 후 재기동 전까지 최소한 유지해야 하는 정지 시간

[비용]
용어                      영문                       단위       의미
기동비용                  Startup Cost               원/회      발전기를 켤 때 드는 비용 (열간·온간·냉간별)

[배출]
용어                      영문                       단위       의미
이산화탄소 환산량         CO2eq                      tCO2eq     다른 온실가스까지 CO2 기준으로 환산해 합친 값

[찾은 곳]
설비용량(정격용량) = 최대발전용량(p_max_mw) 으로 사용
- 찾은 곳: EPSIS > 발전설비 > 연료원별
  https://epsis.kpx.or.kr/epsisnew/selectEkpoBftChart.do?menuId=020100
- 형태: 파일 (화면의 CSV 내려받기, API 아님)
- 저장 경로: data/csv/연료원별_설비용량_utf8_20250701_20260801.csv
- 찾은 내용: 연료원별 설비용량 (단위 MW), 월 단위, 2026/08 -> 2025/07 내림차순 14행
- 조건: 회원구분/급전방식/사업구분/지역 모두 합계
- 컬럼: 기간, 회원구분, 급전방식, 사업구분, 지역, 원자력, 유연탄, 무연탄, 유류, LNG, 양수,
        신재생(연료전지, 석탄가스화, 태양, 풍력, 수력, 해양, 바이오, 폐기물), 기타, 합계
- 파일 형태: 헤더 2줄 (2번째 줄은 신재생 하위 항목명), UTF-8 (BOM 없음), 원본(CP949)에서 기간 밖 행만 제거
- 주의: 값이 소수로 내려옴 (예: 유연탄 40366.676999999996), 화면 표시는 반올림 값
- 주의: 구역전기사업설비, 자가용전기설비는 설비용량 산정 제외 (화면 주석)
- 주의: 정격용량 합계라 정비 중인 호기도 포함됨 (실제 운전 상한보다 클 수 있음)
- 참고(발전기별): EPSIS > 발전설비 > 발전기별 > 발전기 현황, 기준일자 2026-08-05 시점 값만 제공
  https://epsis.kpx.or.kr/epsisnew/selectEkpoBcrGrid.do?menuId=020901

최소발전용량(p_min_mw) :: 발전기 마다 다르며, 한전에 등록하기 위한 최소 용량이 정해져 있지 않음 (4단계로 나누고만 있음), 어느 정도 임이로 정해야 할 듯
- 찾은 곳 1: 기후솔루션 「재생에너지 고속도로의 과속방지턱: 화력발전기 최소발전용량」 표2 (PDF)
  https://content.forourclimate.org/files/research/BSjRQ8e.pdf
  - 가스: 평균 48% (범위 17~68%)  / 정격 대비, 한전 발전자회사 화력발전기
  - 석탄: 평균 60% (범위 50~73%)
- 찾은 곳 2: 기후솔루션·PLANiT 「제주 출력제어 사례로 본 최소발전용량 하향 조정의 편익」 5쪽 표2 (PDF)
  https://content.forourclimate.org/files/research/aunioBf.pdf
  (원출처: 전력시장운영규칙 개정 제24-긴급 3차, '24.4.1 부)
  발전기        최대발전용량(MW)  최소발전용량(MW)  하한치(MW)
  남제주#1,2    94                60                39
  제주기력#2,3  73                42                28
  제주내연#1,2  39                26                24
  제주복합#1,2  123               78                58
- 찾은 곳 3: 이투뉴스 「최근 2년간 대형원전 4회 주파수 출력감발 운전」 2022-03-10 (기사)
  https://www.e2news.com/news/articleView.html?idxno=240228
  - 신고리 3,4호기: 580MWe 감발 (2020-05-03, 2020-09-30~10-05)
  - 신고리 4호기: 290MWe 감발 (2021-09-19~23) / 정격 1,400MW
  - 신고리 1,2,4호기: 300MWe 감발 (2021-09-07~10)
- 찾은 곳 4: 전기신문 2025-05-09 (기사)
  https://www.electimes.com/news/articleView.html?idxno=354535
  - 원전 감발: 출력 대비 80% 기준, 일부 70% 수준
- 찾은 곳 5: 비용평가세부운영규정(전문)_20161101.pdf 제9장 9.3.1 (PDF)
  - 복합발전기 최소발전용량: 운전형태별(GT 또는 CC) 총 용량의 30% 이상

출력증가율 / 출력감소율 (ramp_up_mw_per_h / ramp_down_mw_per_h) :: 발전기 마다 다름 
- 찾은 곳 1: 에너지경제연구원 「전력계통 유연성 강화 방안」 2018-03-30, 18쪽 <전원별 특성 자료(평균)> (PDF)
  https://www.keei.re.kr/keei/download/seminar/180330/DI180330_a02.pdf
  저장 경로: docs/pdf/KEEI_전력계통_유연성_강화방안_20180330.pdf
  구분          양수   복합   기력   집단   원자력
  최대출력(MW)  294    610    888    198    1,161
  최소출력(MW)  79     304    390    67     774
  기동시간      5분    4시간  7시간  6시간  8시간
  증발(MW/분)   135    18     9      3      -
  감발(MW/분)   135    18     9      3      -
- 찾은 곳 2: 비용평가세부운영규정(전문)_20161101.pdf 제9장 9.3.2.2 (PDF)
  저장 경로: docs/pdf/비용평가세부운영규정(전문)_20161101.pdf
  - 석탄발전소: 정격용량의 3.0%/분 이상
  - 중유발전소: 정격용량의 4.5%/분 이상
  - 가스터빈 발전소: 정격용량의 5.0%/분 이상
- 찾은 곳 3: 이투뉴스 2022-03-10 (기사, PDF 없음)
  https://www.e2news.com/news/articleView.html?idxno=240228
  - 원자력 감발 속도: 시간당 3% 이내
- 주의: 찾은 곳 1 은 MW/분, 발전기 1대 평균 (설정은 MW/h)

최소운전시간 / 최소정지시간 (min_up_h / min_down_h)
- 직접 값 없음: data.go.kr, EPSIS 에 해당 데이터 없음 (2026-10-04 확인)
  - data.go.kr 검색: 최소운전시간(관련 없는 5건), 최소정지시간(0건), 발전기 기술특성(관련 없는 2건), 발전기 기동정지(관련 없는 1건)
  - EPSIS: 사이트맵에 발전기 기술특성 메뉴 없음, 자료실 게시판(전력시장/전력계통/관련법규) 첫 페이지에 해당 자료 없음
- 찾은 곳 1 (정의만): docs/pdf/비용평가세부운영규정(전문)_20161101.pdf 제9장 9.3.3
  - 최소운전시간: 계통 연결 이후 분리될 수 있기까지의 최소 시간간격 [Hr]
  - 최소정지시간: 계통 분리 이후 연결될 수 있기까지의 최소 시간간격 [Hr]
- 찾은 곳 2 (참고, 기동시간): 전력거래소 「2012년도 발전설비현황」 부록 Ⅱ. 발전원별 특성 2. 발전원별 기동시간, 인쇄 316쪽 (PDF 15쪽)
  https://kpx.or.kr/boardDownload.es?bid=0045&list_no=51375&seq=16886
  저장 경로: docs/pdf/KPX_2012년도_발전설비현황_부록.pdf
  발전원            COLD(지시-전출력)        WARM                    HOT
  LNG복합 가스터빈  22분~1시간 (상태 구분 없음)
  LNG복합 스팀터빈  3시간25분~7시간15분      1시간54분~4시간13분     1시간18분~2시간45분
  유연탄화력        10시간30분~32시간30분    5시간25분~14시간        2시간18분~8시간30분
  무연탄화력        7시간15분~40시간         6시간45분~23시간        4시간30분~16시간
  석유화력          8시간10분~18시간30분     4시간50분~9시간40분     3시간10분~9시간5분
  양수              3분~7분30초 (상태 구분 없음)
  일반수력          3분~5분 (상태 구분 없음)
  경수로            158시간~339시간          91시간~150시간          76시간~123시간
  중수로            67시간~77시간            47시간~51시간           25시간~27시간
- 찾은 곳 3 (참고, 평균 기동시간): docs/pdf/KEEI_전력계통_유연성_강화방안_20180330.pdf 18쪽
  - 양수 5분, 복합 4시간, 기력 7시간, 집단 6시간, 원자력 8시간
- 주의: 찾은 곳 2, 3 은 기동시간이며 최소운전/정지시간 자체가 아님

==== data.go.kr / EPSIS 우선 탐색 결과 (2026-10-04, data.go.kr AI검색 사용, 기존 내용은 위에 그대로 둠) ====

[파일] 한국전력거래소_연료원별 시간대별 설비용량 및 전력거래량 (data.go.kr 15127395)
- 명세: https://www.data.go.kr/data/15127395/fileData.do
- 저장 경로: data/csv/연료원별_시간대별_설비용량_전력거래량_utf8_20250701_20251231.csv
  (원본 2025-01-01~2025-12-31 중 2025-07-01 이후만, 113,784행, CP949 -> UTF-8)
- 컬럼: 거래일자, 거래시간(01~24), 연료원별(26종), 설비용량(MW), 전력거래량(MWh)
- 주의: 연간 갱신이라 2026-01-01~2026-08-01 구간 없음 (차기 등록 예정 2027-08-13)
- 주의: 전력시장 거래 발전기만 포함, 전력거래량은 송전단 기준
- 같은 자료가 EPSIS 자료실 > 전력시장 게시판에도 있음 ("2025년도 연간 연료원별 시간대별 설비용량 및 전력거래량")
- API 로도 제공 (같은 데이터):
  스웨거: https://infuser.odcloud.kr/oas/docs?namespace=15127395/v1
  GET https://api.odcloud.kr/api/15127395/v1/uddi:af29850e-c2aa-4341-9b8b-ce6da5252913?page=1&perPage=10&serviceKey=${DATA_GO_KR_SERVICE_KEY}
  파라미터: page(기본 1), perPage(기본 10), returnType(JSON/XML), serviceKey (또는 Authorization 헤더)
  응답: page, perPage, totalCount, currentCount, matchCount, data
  (2023년: uddi:c3c4a685-8ecd-4854-9781-9724e83a7f9b, 2024년: uddi:54f28932-fcc4-4315-a44c-f480e808a84e)
  호출 테스트는 안 함

위 파일에서 계산한 값 (2025-07-01~2025-12-31, 연료원 합계 기준, 시간당 MWh = 평균 MW 로 봄)
  연료원   설비용량(MW)        최소거래량  최대거래량  시간당 최대증가  시간당 최대감소  최소/설비용량
  원자력   26,050              15,710.4    20,755.8    189.4            843.7            60.3%
  유연탄   41,338.6~41,339.8   6,119.8     30,178.7    3,117.7          2,829.9          14.8%
  무연탄   400                 0.0         353.2       41.9             76.1             0%
  LNG      46,201.8~48,378.8   4,837.8     38,033.3    6,814.8          5,502.5          10.0%
  중유     65.2                0.0         42.2        17.8             21.2             0%
  양수     4,700               0.0         3,896.9     2,821.3          1,891.3          0%
  수력     1,609.9             35.6        1,101.4     737.3            838.6            2.2%
- 설비용량(p_max_mw): 설비용량(MW) 컬럼 그대로
- 최소발전용량(p_min_mw): 최소거래량 = 기간 중 실제로 가장 낮았던 시간대 값 (기술적 최소출력이 아니라 실적)
- 출력증가율/감소율(ramp_*_mw_per_h): 연속 두 시간대 거래량 차이의 최댓값 (실적, 발전기 능력치 아님)

[API] 한국남동발전㈜_시간대별 화력 발전실적 현황 (data.go.kr 15130618) - 호기별 시간대별 값
- 명세: https://www.data.go.kr/data/15130618/openapi.do
- GET https://apis.data.go.kr/B551893/fire-power-by-hour/list?serviceKey=${DATA_GO_KR_SERVICE_KEY}&page=1&size=10&startD=<조회시작일>&endD=<조회종료일>
- 파라미터: serviceKey(필수), startD(필수, 조회시작일), endD(필수, 조회종료일), page, size
- 응답(JSON) body.content: dgenYmd(년월일), ippt(발전소코드), hogi(호기코드), ipptNam(발전소명),
  qhorGen01~qhorGen24(01시~24시), qsum(총량), qavg(평균), qvodMaxS(최대 시간별), qvodMinS(최소 시간별), qvodMax(최대), qvodMin(최소)
- 활용신청: 개발단계 자동승인, 개발계정 10,000건
- 쓸 수 있는 곳: 호기별 최소출력/최대출력, 시간당 출력 변화, 연속 운전/정지 시간(최소운전/정지시간 실적)
- 미확인: 날짜 형식(startD/endD), 실제 응답, 포함 발전소 목록 (활용신청 필요해서 호출 안 함)

항목별 결과
- 설비용량 / 최대발전용량: 찾음 (위 파일, 그리고 앞서 저장한 data/csv/연료원별_설비용량_utf8_20250701_20260801.csv)
  참고 데이터: 한국전력거래소_월별 연료원별 발전설비 https://www.data.go.kr/data/15046115/fileData.do
               한국전력거래소_연도별 발전기별 발전설비 https://www.data.go.kr/data/15046119/fileData.do
               한국전력거래소_발전기별 세부내역 https://www.data.go.kr/data/15150482/fileData.do
               한국수력원자력(주)_중앙급전발전기 현황 (발전원, 발전기명, 설비용량 MW, 40행) https://www.data.go.kr/data/15101273/fileData.do
- 최소발전용량: 기술적 최소출력 데이터는 두 사이트에 없음. 실적 기반 값은 위 파일(연료원 합계)과 위 API(호기별)로 계산 가능
- 출력증가율 / 출력감소율: 능력치 데이터는 두 사이트에 없음. 실적 기반 값은 위 파일과 위 API로 계산 가능
- 최소운전시간 / 최소정지시간: 두 사이트에 없음 (2회 시도). 호기별 실적은 위 API로 계산 가능
  관련 데이터: 한국동서발전(주)_발전기별 연도별 기동횟수 (연도, 사업소, 호기, 기동횟수, 165행) https://www.data.go.kr/data/15087278/fileData.do
               한국동서발전(주)_발전소별 정지실적 정보 (연도별 정지 유형별 횟수, 21행) https://www.data.go.kr/data/15104626/fileData.do
- 기동비용: 두 사이트에 없음 (2회 시도)
  data.go.kr 검색 "기동비용" 0건, "기동 비용 정산" 0건, AI검색 결과도 연료비용/정산단가/기동횟수뿐
  확인한 데이터: 전력계통 운영 보조서비스 정산금(15069364, 기동비 컬럼 없음), 한국서부발전(주)_발전소별 발전원가(15106281, 기동비 컬럼 없음)

==== 인터넷 추가 탐색 결과 (2026-10-04, 기존 내용은 위에 그대로 둠) ====

[논문] KPG 193: A Synthetic Korean Power Grid Test System for Decarbonization Studies (Geonho Song, Jip Kim, arXiv 2411.14756v2, 2025)
- 찾은 곳: https://arxiv.org/abs/2411.14756v2  (PDF 4쪽 TABLE IV. Generator Parameters by Fuel Type)
- 저장 경로: docs/pdf/KPG193_arXiv_2411.14756v2.pdf
- 데이터 저장소: https://sites.google.com/view/ego-lab/resources/kpg-test-system
- 한국 계통을 본뜬 시험계통용 발전기 파라미터 (연료원별)
  Fuel     Min.Gen.[%Cap.]  Ramp Rate[%Cap./hr]  UT[h]  DT[h]  Startup Cost[KRW/MW]  Cg(1)[KRW/MWh]    Cg(0)[KRW]
  LNG      52%              100                  4      3      53,862                36,872~70,956     637,657~6,531,339
  Coal     40%              66                   6      12     12,606                22,912~27,174     1,227,022~2,629,634
  Nuclear  95%              18                   8      12     -                     3,339~8,292       0
  (Cg(2)[KRW/MW2h]: LNG 2.1215~6.6711, Coal 25.6102~30.5675, Nuclear 1.6591~3.0364)
- 항목 대응
  - 최소발전용량(p_min_mw): Min. Gen. (설비용량 대비 %)
  - 출력증가율/감소율(ramp_*_mw_per_h): Ramp Rate (설비용량 대비 %/시간, 증가/감소 구분 없음)
  - 최소운전시간(min_up_h): UT / 최소정지시간(min_down_h): DT
  - 기동비용(startup_cost_krw): Startup Cost (KRW/MW, 설비용량을 곱해야 원/회)
- 주의: 한국 실측값이 아님. 논문에 "UC parameters and generation cost coefficients are not available" 라고 적혀 있고,
        미국 자료(NREL WWSIS-2 [31], ISO New England 8-zone test system [32])에서 가져와 무작위화·수정한 값 (방법 [9])
- 주의: 원자력 기동비용은 "-" (값 없음)

확인했으나 값이 없던 곳
- PyPSA-Korea (Energy Reports 2025, https://github.com/RogerKwak/PyPSA-Korea): 석탄/원자력/CCGT 파라미터는 Lyden et al.(2024) 값을 썼다고만 하고 본문에 표 없음
- Reserve-Constrained Unit Commitment ... in Korean Power System (Energies 15(7), 2022, https://www.mdpi.com/1996-1073/15/7/2386): 제약식만 있고 수치 없음

[가상 발전기 선정하기]
발전원별 설비용량(MW) 상위 5 (2026-10-05 조사)
- 기준: 최대 생산량 = 설비용량(MW). 호기별 발전량은 공개 자료 없음
- (미확인) = 이번 조사에서 출처로 직접 확인하지 못한 값. EPSIS 발전기별 현황 CSV 로 대조 필요
  https://epsis.kpx.or.kr/epsisnew/selectEkpoBcrGrid.do?menuId=020901 (조회형 화면이라 직접 읽지 못함)

- 수력 (전부 미확인)
  충주 1~4호기      각 100MW
  소양강 1·2호기    각 100MW
  - 주의: 6기 동률
- 유류
  5개를 채울 수 없음
  - EPSIS 유류 합계 65.2MW 뿐, 호기 구성 확인 못함
- 유연탄
  태안 9·10호기           각 1,050MW
  삼척블루파워 1·2호기    각 1,050MW  (총 2,100MW 를 2기로 나눈 값)
  고성하이 1·2호기        각 1,040MW  (총 2,080MW 를 2기로 나눈 값, 5위 동률)
  강릉안인 1·2호기        각 1,040MW  (총 2,080MW 를 2기로 나눈 값, 5위 동률)
  - 찾은 곳: 한국서부발전 발전설비 현황 (2026-05-20 기준) https://www.iwest.co.kr/iwest/555/subview.do
             https://en.wikipedia.org/wiki/List_of_power_stations_in_South_Korea
- 원자력
  새울 1·2호기 (구 신고리 3·4호기)   각 1,400MW  APR1400
  신한울 1·2호기                     각 1,400MW  APR1400
  새울 3호기                         1,400MW     APR1400 (상업운전 여부 미확인, 2026-05 기사 기준 9월 말 예정)
  - 찾은 곳: https://www.heraldk.com/article/2025122919283262288
             https://www.mt.co.kr/tech/2026/05/20/2026052008301264636
  - APR1400 램프율 (1,400MW 환산)
    탄력운전 최대 출력 변동률   30%/h       420 MW/h   한국원자력학회 2026 춘계 워크숍 「APR1400 탄력운전」 https://www.kns.org/boards/download/33888
    일일 부하추종 모의 100→50%  2~3시간     350 또는 약 233 MW/h   KNS 논문 (2022, 2024)
    설계 능력 (System 80+)      ±5%/분      70 MW/분   https://proceedings.cns-snc.ca/index.php/pcns/article/download/3245/3244/3280
    실제 감발 운전              3%/h 이내   42 MW/h    이투뉴스 2022-03-10
    - 주의: 30%/h 는 검색 결과 문구만 확인 (원문 열리지 않음, 적용 조건 미확인)
    - 주의: ±5%/분 은 APR1400 이 아니라 참조 노형 System 80+ 의 설계값
    - 주의: 실제 운전에서 확인되는 값은 3%/h 뿐, 나머지는 설계·연구상 능력치
- 양수
  예천 1·2호기      각 400MW
  산청 1·2호기      각 350MW  (총 700MW 를 2기로 나눈 값)
  청송 1·2호기      각 300MW  (총 600MW 를 2기로 나눈 값, 5위 동률)
  무주 1·2호기      각 300MW  (위와 같음)
  삼랑진 1·2호기    각 300MW  (위와 같음)
  - 찾은 곳: https://news.tf.co.kr/read/economy/2323327.htm (예천 400MW 2기)
             https://ko.wikipedia.org/wiki/양수_발전 (발전소별 총용량)
- 가스 (복합 1블록 GT+ST 기준)
  울산GPS           1,227MW (미확인)
  통영에코파워      1,012MW  GT 2기 + ST 1기, 2024-10-29 상업운전
  여주천연가스      1,004MW (미확인)
  신평택            940MW
  파주문산 1·2호기  각 약 900MW (미확인)
  - 찾은 곳: https://www.smarttoday.co.kr/ko-kr/articles/63125
             https://www.inews24.com/view/1026321
- 국내탄
  동해 1·2호기      각 200MW (미확인)
  - 2기뿐. EPSIS 무연탄 400MW 와 일치
- 신재생 (태양광·풍력 제외)
  태안 IGCC         380MW  석탄가스화
  시화호 조력       254MW
  영동에코 2호기    200MW  바이오매스 (1~2호기 합계 325MW 만 확인)
  영동에코 1호기    125MW  바이오매스 (위와 같음)
  SGC그린파워       100MW  바이오매스
  - 제외: 광양그린에너지 220MW (준공 여부 미확인)
  - 찾은 곳: https://www.iwest.co.kr/iwest/555/subview.do
             윤미향의원 2023년 국정감사 정책보고서 「대한민국 산림의 땔감화」 [표 5] 전국 주요 바이오매스 발전소 현황 (전력거래소 사이트 게시) https://www.kpx.or.kr/boardDownload.es?bid=0048&list_no=72212OOO202404111334002042&seq=2
- 태양광 (발전기가 아니라 단지 기준)
  신안 안좌         288MW
  신안 비금         200MW  (2024-11 기준 준공 직전)
  신안 지도·사옥도  150MW  2022-01-26 상업운전
  신안 임자         99.9MW 2022-09 상업운전
  솔라시도          98MW (미확인)
  - 찾은 곳: https://www.mt.co.kr/industry/2024/11/02/2024110115033217577
             https://www.etoday.co.kr/news/view/2100361
- 풍력 (단지 기준, 영광 외 용량은 미확인)
  낙월해상          364.8MW (일부만 상업운전 중)
  제주한림해상      100MW
  전남해상          약 96~99MW
  강원풍력          98MW
  영광풍력          79.6MW  (육상 45.1MW + 해상 34.5MW)
  - 찾은 곳: https://www.smarttoday.co.kr/ko-kr/articles/98675
             https://www.mt.co.kr/economy/2019/04/04/2019040414064544327

- 주의: 원자력, 유연탄, 양수, 수력은 같은 용량의 호기가 여러 대라 상위 5개가 대표 기종 1~2개로 좁혀짐
- 주의: 태양광·풍력은 기동·정지 대상이 아니고, 유류는 규모가 작음


[ 자료 조사 ]

- 표기: null = 조사했으나 값을 찾지 못함 / 0 = 해당 항목 자체가 없는 값 (처음에는 '없음' 으로 적었다가 0 으로 통일)
- u_init, init_state_h 는 모델 초기 조건이라 조사 값이 아님 -> 0 (실제 초기 상태는 계획 수립 때 따로 정할 것)
- 주의: 원자력·수력·양수 startup_cost 0 (규정상 미적용), 수력·양수 min_up/min_down 0 (기동 3~5분) 은 근거가 있는 0 이고, 나머지 0 은 해당 없음을 뜻함
- 연료원별 % (KPG 193 등) 를 곱해 채우는 작업은 하지 않음 (나중에 적용)
- 발전기(기종)별로 직접 조사된 값만 넣음. (미확인) = 출처로 직접 확인하지 못한 값
- co2_ton_per_mwh: 호기별 값 없음. 석탄 0.991 / 석유 0.782 / LNG 0.549 / 태양광 0.054 / 원자력 0.010 은
  발전원 단위 값 (IAEA 2006, 에너지경제 기사 인용 https://m.ekn.kr/view.php?key=129695, 전과정 기준인지 기사에 명시 없음)
- 최소발전용량, 최소운전/정지시간, 기동비용: 화력 호기별 공개 값 못 찾음 (2026-10-05 2차 탐색 후에도 null)
  기후솔루션 보고서·보도자료: 개별 발전소 최소발전용량은 "영업 비밀이라는 이유로 공개되지 않는다" https://forourclimate.org/ko/newsroom/1110
  확인한 곳: 전력거래소 「발전기 기동비용 산정 및 적용기준 개선방안 연구」(2012, 한국전기연구원) 앞 42쪽까지만 열림, 호기별 표 없음
             https://www.kpx.or.kr/boardDownload.es?bid=0045&list_no=51351&seq=16281
             docs/pdf/KPX_2012년도_발전설비현황_부록.pdf (발전원별 기동시간만 있음)

- 2차 탐색으로 채운 값 (2026-10-05, 인터넷 + docs/pdf)
  - 원자력·수력·양수 startup_cost_krw = 0
    docs/pdf/비용평가세부운영규정(전문)_20161101.pdf 3.4.1.1 "원자력 및 수력·양수발전기는 가격결정 발전계획 수립시 기동비용을 적용하지 않는다"
  - 원자력 p_min 1,120 / ramp 42: 현행 운전 기준 (출력 80% 까지 조절, 1시간에 3% 씩)
    https://pressian.com/pages/articles/2026022411161701811
    같은 기사: 2032년 목표는 50% 까지 출력 제어 (= 700MW). 탄력운전 최대 변동률 30%/h (= 420 MW/h) 는 기술개발 능력치
    - 주의: 1차 작성 때 ramp 를 420 으로 적었으나 p_min 과 기준을 맞추려고 현행 값 42 로 바꿈
  - 양수 ramp 6,000 = 100 MW/분 x 60 (전력거래소 「계통신뢰도를 고려한 양수발전기 운영방안에 관한 연구」 2013-03, 표 2-3 "100~200MW/분" 의 하한)
    https://new.kpx.or.kr/boardDownload.es?bid=0045&list_no=51427&seq=16063
    같은 보고서: 기동 3~5분 (표 2-2), 효율 70~80% (표 2-1), 호기 구성 (표 2-4) 산청 350MW x 2, 청송 300MW x 2, 예천 400MW x 2
    - 주의: 호기 용량보다 커서 1시간 단위 모델에서는 사실상 제약 없음
  - 유연탄 ramp = 정격용량 x 3.0%/분 x 60, 가스 ramp = 정격용량 x 5.0%/분 x 60
    docs/pdf/비용평가세부운영규정(전문)_20161101.pdf 9.3.2.2 (석탄발전소 3.0%/분 이상, 가스터빈 발전소 5.0%/분 이상)
    - 주의: 호기 실측값이 아니라 규정상 하한. 정격용량에 % 를 곱한 값이라 "나중에 % 적용" 범위에 해당하면 null 로 되돌릴 것
    - 주의: 1시간 단위 모델에서는 p_max 보다 커서 사실상 제약 없음
    - 동해(국내탄)는 2005-01-23 이전 진입 발전기라 같은 조항의 예외 대상일 수 있어 null 유지
  - 수력 co2 는 직접 배출이 없는 설비라 null -> 없음 으로 바꿈
  - 수력·양수 min_up_h / min_down_h = 0
    기동 3~5분 (docs/pdf/KPX_2012년도_발전설비현황_부록.pdf: 일반수력 3분~5분, 양수 3분~7분30초) 이라 1시간 단위 모델에서는 제약 없음
    - 주의: 조사된 값이 아니라 위 기동시간을 근거로 0 으로 정한 값 (규정상 제출 항목이지만 공개 값은 없음)
- 찾았지만 호기 값이 아니라서 넣지 않은 것 (% 적용 때 쓸 후보)
  - docs/pdf/KEEI_전력계통_유연성_강화방안_20180330.pdf <유연성 제공 자원 특성>: 양수 최소출력 28.3%, 증감발 130~300MW/분, 기동 5분 / 가스터빈 최소출력 25%, 증감발 88MW/분, 기동(Hot) 20~40분
  - 같은 규정 9.3.1.7: 복합발전기 최소발전용량은 운전형태별(GT 또는 CC) 총 용량의 30% 이상
  - Siemens SGT-8000H 자료: 2x1 복합 880MW, 효율 60% 초과 (60Hz 기종의 ramp, 최소부하 수치는 없음). 여주는 Siemens 가스터빈이나 기종명은 확인 못함
- 2차 탐색에서도 값이 없던 곳
  - 전력거래소 비용평가 세부운영규정 2025.12 (앞부분만 열림), 기후솔루션 최소발전용량 보고서 (호기별 표 없음),
    KEEI 이슈페이퍼 KIP1901 (발전기 특성 표 없음), 「화력 발전소의 유연 운전 기술」(DBpia, 초록만 열림), 한수원 수력·양수 현황 (열리지 않음)
  - 비용평가 규정 [별지]: 기동비용, 최소운전/정지시간, 출력증가/감소율 제출 양식만 있고 값은 없음

- 추정으로 채운 값 (값 뒤에 "// 추정", 2026-10-05). 위 자료의 연료원별 값을 호기 용량에 적용한 것
  - 유연탄: p_min = p_max x 60% (기후솔루션 표2 석탄 평균), min_up 6 / min_down 12 / startup = p_max x 12,606원 (KPG 193 Coal)
            ramp = p_max x 3.0%/분 x 60 (비용평가 규정 9.3.2.2 하한, 앞서 넣은 값에 표시만 추가)
  - 가스: p_min = p_max x 48% (기후솔루션 표2 가스 평균), min_up 4 / min_down 3 / startup = p_max x 53,862원 (KPG 193 LNG)
          ramp = p_max x 5.0%/분 x 60 (비용평가 규정 9.3.2.2 하한, 앞서 넣은 값에 표시만 추가)
  - 원자력: min_up 8 / min_down 12 (KPG 193 Nuclear)
  - 양수: p_min = p_max x 28.3% (KEEI <유연성 제공 자원 특성> 양수 최소출력)
  - 수력: ramp = p_max (기동 3~5분이라 1시간 안에 전 구간 이동 가능하다고 봄)
  - 국내탄: 유연탄과 같은 비율. 단 ramp = p_max x 66%/h (KPG 193 Coal, 규정 3.0%/분 은 2005 이전 진입 발전기 예외 가능성 때문에 쓰지 않음)
  - 신재생 중 태안 IGCC·영동에코·SGC그린파워: 석탄 계열 보일러로 보고 국내탄과 같은 비율 적용 (근거 가장 약함)
  - 주의: KPG 193 값은 한국 실측이 아니라 미국 자료 기반 값
  - 주의: 기후솔루션 60% / 48% 대신 KPG 193 (석탄 40%, LNG 52%) 을 쓰면 p_min 이 달라짐
  - 주의: 표기는 요청대로 "// 추정". YAML 주석 기호는 # 이라 config 로 옮길 때는 바꿔야 함
- 마지막 null 처리 (2026-10-05 3차 탐색)
  - 바이오매스 3개 (영동에코 1·2호기, SGC그린파워) co2 = 0: 온실가스 산정에서 바이오매스 연소분은 0 으로 계산하는 관행을 따름
  - 수력 p_min = p_max x 60%  // 추정
    Statkraft: "Most hydropower turbines should not run below 60% power production capacity"
    https://explained.statkraft.com/newsroom/explained/renewable-new-turbine-technology-will-make-hydropower-more-flexible
    - 주의: 충주·소양강 호기 값이 아니라 수차 일반론. 국내 수력 호기별 최소출력은 찾지 못함
  - 태안 IGCC co2 = 0.991 x 0.9 = 0.892  // 추정
    서울신문 2016-06-07: IGCC 발전 효율 약 42% 로 석탄화력 대비 2%p 높고, "발전 효율이 2% 올라가면 이산화탄소 발생량은 10% 줄어든다"
    https://www.seoul.co.kr/news/economy/2016/06/07/20160607022004
    에너지타임즈: 태안 IGCC 효율 42.3%, 기존 석탄발전 38~40% https://www.energytimes.kr/news/articleView.html?idxno=53239
    - 주의: 태안 IGCC 의 CO2 배출계수 실측값은 찾지 못함. 효율 차이만으로 계산하면 감소폭은 약 5% (0.94) 로 기사 수치보다 작음
  - 확인했으나 값이 없던 곳: NETL Gasifipedia (부산물 비교만 있음), SINTEF Francis 수차 리뷰 (초록에 수치 없음), 한국에너지신문 태안 IGCC 기사 (열리지 않음)
- null 없음

generators:
  # ---- 수력 (충주 4기, 소양강 2기 모두 100MW 동률, 5개만 적음) ----
  # 찾은 곳: K-water 한강유역본부 시설현황 https://www.kwater.or.kr/busi/sub02/facilitiespresentPage.do?s_mid=1511
  #          충주댐 41만2천kW (10만kW급 4기), 소양강댐 20만kW (10만kW x 2대)
  - id: hydro_chungju_1  # 충주 1호기
    fuel: hydro
    p_max_mw: 100
    p_min_mw: 60  // 추정
    ramp_up_mw_per_h: 100  // 추정
    ramp_down_mw_per_h: 100  // 추정
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0
    u_init: 0
    init_state_h: 0
  - id: hydro_chungju_2  # 충주 2호기
    fuel: hydro
    p_max_mw: 100
    p_min_mw: 60  // 추정
    ramp_up_mw_per_h: 100  // 추정
    ramp_down_mw_per_h: 100  // 추정
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0
    u_init: 0
    init_state_h: 0
  - id: hydro_chungju_3  # 충주 3호기
    fuel: hydro
    p_max_mw: 100
    p_min_mw: 60  // 추정
    ramp_up_mw_per_h: 100  // 추정
    ramp_down_mw_per_h: 100  // 추정
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0
    u_init: 0
    init_state_h: 0
  - id: hydro_chungju_4  # 충주 4호기
    fuel: hydro
    p_max_mw: 100
    p_min_mw: 60  // 추정
    ramp_up_mw_per_h: 100  // 추정
    ramp_down_mw_per_h: 100  // 추정
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0
    u_init: 0
    init_state_h: 0
  - id: hydro_soyanggang_1  # 소양강 1호기 (2호기 동률)
    fuel: hydro
    p_max_mw: 100
    p_min_mw: 60  // 추정
    ramp_up_mw_per_h: 100  // 추정
    ramp_down_mw_per_h: 100  // 추정
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0
    u_init: 0
    init_state_h: 0
  # ---- 유류 ----
  # 없음: 5개를 채울 수 없음. EPSIS 유류 합계 65.2MW, 호기 구성 확인 못함

  # ---- 유연탄 (고성하이 1·2, 강릉안인 1·2 가 1,040MW 로 5위 동률) ----
  # 찾은 곳: 한국서부발전 발전설비 현황 https://www.iwest.co.kr/iwest/555/subview.do (태안 9·10 1,050MW x 2)
  #          https://www.sedaily.com/article/14010031 (삼척블루파워 1호기 1,050MW 2024-05, 2호기 1,050MW 2025-01-01 상업운전, 초초임계압)
  - id: coal_taean_9  # 태안 9호기
    fuel: bituminous_coal
    p_max_mw: 1050
    p_min_mw: 630  // 추정
    ramp_up_mw_per_h: 1890  // 추정
    ramp_down_mw_per_h: 1890  // 추정
    min_up_h: 6  // 추정
    min_down_h: 12  // 추정
    startup_cost_krw: 13236300  // 추정
    co2_ton_per_mwh: 0.991
    u_init: 0
    init_state_h: 0
  - id: coal_taean_10  # 태안 10호기
    fuel: bituminous_coal
    p_max_mw: 1050
    p_min_mw: 630  // 추정
    ramp_up_mw_per_h: 1890  // 추정
    ramp_down_mw_per_h: 1890  // 추정
    min_up_h: 6  // 추정
    min_down_h: 12  // 추정
    startup_cost_krw: 13236300  // 추정
    co2_ton_per_mwh: 0.991
    u_init: 0
    init_state_h: 0
  - id: coal_samcheok_blue_1  # 삼척블루파워 1호기
    fuel: bituminous_coal
    p_max_mw: 1050
    p_min_mw: 630  // 추정
    ramp_up_mw_per_h: 1890  // 추정
    ramp_down_mw_per_h: 1890  // 추정
    min_up_h: 6  // 추정
    min_down_h: 12  // 추정
    startup_cost_krw: 13236300  // 추정
    co2_ton_per_mwh: 0.991
    u_init: 0
    init_state_h: 0
  - id: coal_samcheok_blue_2  # 삼척블루파워 2호기
    fuel: bituminous_coal
    p_max_mw: 1050
    p_min_mw: 630  // 추정
    ramp_up_mw_per_h: 1890  // 추정
    ramp_down_mw_per_h: 1890  // 추정
    min_up_h: 6  // 추정
    min_down_h: 12  // 추정
    startup_cost_krw: 13236300  // 추정
    co2_ton_per_mwh: 0.991
    u_init: 0
    init_state_h: 0
  - id: coal_goseong_hi_1  # 고성하이 1호기 (총 2,080MW 를 2기로 나눈 값)
    fuel: bituminous_coal
    p_max_mw: 1040
    p_min_mw: 624  // 추정
    ramp_up_mw_per_h: 1872  // 추정
    ramp_down_mw_per_h: 1872  // 추정
    min_up_h: 6  // 추정
    min_down_h: 12  // 추정
    startup_cost_krw: 13110240  // 추정
    co2_ton_per_mwh: 0.991
    u_init: 0
    init_state_h: 0
  # ---- 원자력 (전부 APR1400) ----
  # 찾은 곳: https://www.heraldk.com/article/2025122919283262288
  # p_min 1,120 = 현행 출력 조절 하한 80%, ramp 42 = 3%/h (현행 운전 기준, 위 2차 탐색 참고)
  # 능력치: 탄력운전 최대 출력 변동률 30%/h = 420 MW/h (한국원자력학회 2026 춘계 워크숍 「APR1400 탄력운전」, 검색 결과 문구만 확인)
  #         https://www.kns.org/boards/download/33888
  # startup_cost 0 = 규정상 기동비용 미적용
  - id: nuclear_saeul_1  # 새울 1호기 (구 신고리 3호기)
    fuel: nuclear
    p_max_mw: 1400
    p_min_mw: 1120
    ramp_up_mw_per_h: 42
    ramp_down_mw_per_h: 42
    min_up_h: 8  // 추정
    min_down_h: 12  // 추정
    startup_cost_krw: 0
    co2_ton_per_mwh: 0.01
    u_init: 0
    init_state_h: 0
  - id: nuclear_saeul_2  # 새울 2호기 (구 신고리 4호기)
    fuel: nuclear
    p_max_mw: 1400
    p_min_mw: 1120
    ramp_up_mw_per_h: 42
    ramp_down_mw_per_h: 42
    min_up_h: 8  // 추정
    min_down_h: 12  // 추정
    startup_cost_krw: 0
    co2_ton_per_mwh: 0.01
    u_init: 0
    init_state_h: 0
  - id: nuclear_shinhanul_1  # 신한울 1호기
    fuel: nuclear
    p_max_mw: 1400
    p_min_mw: 1120
    ramp_up_mw_per_h: 42
    ramp_down_mw_per_h: 42
    min_up_h: 8  // 추정
    min_down_h: 12  // 추정
    startup_cost_krw: 0
    co2_ton_per_mwh: 0.01
    u_init: 0
    init_state_h: 0
  - id: nuclear_shinhanul_2  # 신한울 2호기
    fuel: nuclear
    p_max_mw: 1400
    p_min_mw: 1120
    ramp_up_mw_per_h: 42
    ramp_down_mw_per_h: 42
    min_up_h: 8  // 추정
    min_down_h: 12  // 추정
    startup_cost_krw: 0
    co2_ton_per_mwh: 0.01
    u_init: 0
    init_state_h: 0
  - id: nuclear_saeul_3  # 새울 3호기 (상업운전 여부 미확인)
    fuel: nuclear
    p_max_mw: 1400
    p_min_mw: 1120
    ramp_up_mw_per_h: 42
    ramp_down_mw_per_h: 42
    min_up_h: 8  // 추정
    min_down_h: 12  // 추정
    startup_cost_krw: 0
    co2_ton_per_mwh: 0.01
    u_init: 0
    init_state_h: 0
  # ---- 양수 (청송·무주·삼랑진 6기가 300MW 로 5위 동률) ----
  # 찾은 곳: https://www.newspim.com/news/view/20260517000088
  #          예천: 400MW x 2기, 급전 지시 후 3~5분 이내 발전 시작, 발전 약 8.4시간 / 양수 약 10시간, 효율 약 84%
  #          https://ko.wikipedia.org/wiki/양수_발전 (산청 700MW, 청송 600MW 총용량)
  # co2: 직접 배출이 없는 설비라 없음
  - id: pumped_yecheon_1  # 예천 1호기
    fuel: pumped
    p_max_mw: 400
    p_min_mw: 113.2  // 추정
    ramp_up_mw_per_h: 6000
    ramp_down_mw_per_h: 6000
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0
    u_init: 0
    init_state_h: 0
  - id: pumped_yecheon_2  # 예천 2호기
    fuel: pumped
    p_max_mw: 400
    p_min_mw: 113.2  // 추정
    ramp_up_mw_per_h: 6000
    ramp_down_mw_per_h: 6000
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0
    u_init: 0
    init_state_h: 0
  - id: pumped_sancheong_1  # 산청 1호기
    fuel: pumped
    p_max_mw: 350
    p_min_mw: 99  // 추정
    ramp_up_mw_per_h: 6000
    ramp_down_mw_per_h: 6000
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0
    u_init: 0
    init_state_h: 0
  - id: pumped_sancheong_2  # 산청 2호기
    fuel: pumped
    p_max_mw: 350
    p_min_mw: 99  // 추정
    ramp_up_mw_per_h: 6000
    ramp_down_mw_per_h: 6000
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0
    u_init: 0
    init_state_h: 0
  - id: pumped_cheongsong_1  # 청송 1호기
    fuel: pumped
    p_max_mw: 300
    p_min_mw: 84.9  // 추정
    ramp_up_mw_per_h: 6000
    ramp_down_mw_per_h: 6000
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0
    u_init: 0
    init_state_h: 0
  # ---- 가스 (복합 1블록 GT+ST 기준) ----
  # 찾은 곳: https://www.hankyung.com/article/202412269073h (울산GPS 상업운전 개시, 용량은 본문 확인 못함)
  #          https://www.smarttoday.co.kr/ko-kr/articles/63125 (통영에코파워 1,012MW, GT 2기 + ST 1기, 2024-10-29 상업운전)
  #          https://www.skens.com/yeoju/content/view.do?cate=energy (여주 1,000MW = GT 2대 670MW + ST 1대 330MW, 2023-07-05 상업운전)
  #          https://www.inews24.com/view/1026321 (신평택 940MW)
  - id: lng_ulsan_gps  # 울산GPS, LNG·LPG 겸용 (용량 미확인)
    fuel: lng
    p_max_mw: 1227
    p_min_mw: 589  // 추정
    ramp_up_mw_per_h: 3681  // 추정
    ramp_down_mw_per_h: 3681  // 추정
    min_up_h: 4  // 추정
    min_down_h: 3  // 추정
    startup_cost_krw: 66088674  // 추정
    co2_ton_per_mwh: 0.549
    u_init: 0
    init_state_h: 0
  - id: lng_tongyeong_eco  # 통영에코파워
    fuel: lng
    p_max_mw: 1012
    p_min_mw: 485.8  // 추정
    ramp_up_mw_per_h: 3036  // 추정
    ramp_down_mw_per_h: 3036  // 추정
    min_up_h: 4  // 추정
    min_down_h: 3  // 추정
    startup_cost_krw: 54508344  // 추정
    co2_ton_per_mwh: 0.549
    u_init: 0
    init_state_h: 0
  - id: lng_yeoju  # 여주천연가스 (앞서 적은 1,004MW 는 미확인 값, 사업자 사이트는 1,000MW)
    fuel: lng
    p_max_mw: 1000
    p_min_mw: 480  // 추정
    ramp_up_mw_per_h: 3000  // 추정
    ramp_down_mw_per_h: 3000  // 추정
    min_up_h: 4  // 추정
    min_down_h: 3  // 추정
    startup_cost_krw: 53862000  // 추정
    co2_ton_per_mwh: 0.549
    u_init: 0
    init_state_h: 0
  - id: lng_sinpyeongtaek  # 신평택
    fuel: lng
    p_max_mw: 940
    p_min_mw: 451.2  // 추정
    ramp_up_mw_per_h: 2820  // 추정
    ramp_down_mw_per_h: 2820  // 추정
    min_up_h: 4  // 추정
    min_down_h: 3  // 추정
    startup_cost_krw: 50630280  // 추정
    co2_ton_per_mwh: 0.549
    u_init: 0
    init_state_h: 0
  - id: lng_paju_munsan_1  # 파주문산 1호기 (약 900MW, 미확인)
    fuel: lng
    p_max_mw: 900
    p_min_mw: 432  // 추정
    ramp_up_mw_per_h: 2700  // 추정
    ramp_down_mw_per_h: 2700  // 추정
    min_up_h: 4  // 추정
    min_down_h: 3  // 추정
    startup_cost_krw: 48475800  // 추정
    co2_ton_per_mwh: 0.549
    u_init: 0
    init_state_h: 0
  # ---- 국내탄 (2기뿐) ----
  # 찾은 곳: EPSIS 무연탄 설비용량 400MW 와 일치. 호기별 200MW 는 미확인
  #          https://www.koreascience.or.kr/article/CFKO200735738816309.pdf (동해화력 순환유동층 보일러, 150~200MW 범위로만 언급)
  - id: anthracite_donghae_1  # 동해 1호기 (미확인, co2 는 석탄 공통 값)
    fuel: anthracite
    p_max_mw: 200
    p_min_mw: 120  // 추정
    ramp_up_mw_per_h: 132  // 추정
    ramp_down_mw_per_h: 132  // 추정
    min_up_h: 6  // 추정
    min_down_h: 12  // 추정
    startup_cost_krw: 2521200  // 추정
    co2_ton_per_mwh: 0.991
    u_init: 0
    init_state_h: 0
  - id: anthracite_donghae_2  # 동해 2호기 (미확인, co2 는 석탄 공통 값)
    fuel: anthracite
    p_max_mw: 200
    p_min_mw: 120  // 추정
    ramp_up_mw_per_h: 132  // 추정
    ramp_down_mw_per_h: 132  // 추정
    min_up_h: 6  // 추정
    min_down_h: 12  // 추정
    startup_cost_krw: 2521200  // 추정
    co2_ton_per_mwh: 0.991
    u_init: 0
    init_state_h: 0
  # ---- 신재생 (태양광·풍력 제외) ----
  # 찾은 곳: https://www.iwest.co.kr/iwest/555/subview.do (태안 IGCC 380MW)
  #          윤미향의원 2023년 국정감사 정책보고서 「대한민국 산림의 땔감화」 [표 5] 전국 주요 바이오매스 발전소 현황 (전력거래소 사이트 게시) https://www.kpx.or.kr/boardDownload.es?bid=0048&list_no=72212OOO202404111334002042&seq=2
  #          (영동에코 1~2호기 합계 325MW, SGC그린파워 100MW)
  - id: renew_taean_igcc  # 태안 IGCC (석탄가스화)
    fuel: renewable
    p_max_mw: 380
    p_min_mw: 228  // 추정
    ramp_up_mw_per_h: 250.8  // 추정
    ramp_down_mw_per_h: 250.8  // 추정
    min_up_h: 6  // 추정
    min_down_h: 12  // 추정
    startup_cost_krw: 4790280  // 추정
    co2_ton_per_mwh: 0.892  // 추정
    u_init: 0
    init_state_h: 0
  - id: renew_sihwa_tidal  # 시화호 조력 (미확인, 조석에 따라 발전)
    fuel: renewable
    p_max_mw: 254
    p_min_mw: 0
    ramp_up_mw_per_h: 0
    ramp_down_mw_per_h: 0
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0
    u_init: 0
    init_state_h: 0
  - id: renew_yeongdong_eco_2  # 영동에코 2호기 (바이오매스, 호기별 용량 미확인)
    fuel: renewable
    p_max_mw: 200
    p_min_mw: 120  // 추정
    ramp_up_mw_per_h: 132  // 추정
    ramp_down_mw_per_h: 132  // 추정
    min_up_h: 6  // 추정
    min_down_h: 12  // 추정
    startup_cost_krw: 2521200  // 추정
    co2_ton_per_mwh: 0
    u_init: 0
    init_state_h: 0
  - id: renew_yeongdong_eco_1  # 영동에코 1호기 (바이오매스, 호기별 용량 미확인)
    fuel: renewable
    p_max_mw: 125
    p_min_mw: 75  // 추정
    ramp_up_mw_per_h: 82.5  // 추정
    ramp_down_mw_per_h: 82.5  // 추정
    min_up_h: 6  // 추정
    min_down_h: 12  // 추정
    startup_cost_krw: 1575750  // 추정
    co2_ton_per_mwh: 0
    u_init: 0
    init_state_h: 0
  - id: renew_sgc_greenpower  # SGC그린파워 (바이오매스)
    fuel: renewable
    p_max_mw: 100
    p_min_mw: 60  // 추정
    ramp_up_mw_per_h: 66  // 추정
    ramp_down_mw_per_h: 66  // 추정
    min_up_h: 6  // 추정
    min_down_h: 12  // 추정
    startup_cost_krw: 1260600  // 추정
    co2_ton_per_mwh: 0
    u_init: 0
    init_state_h: 0
  # ---- 태양광 (발전기가 아니라 단지 기준, 기동·정지 항목은 없음) ----
  # 찾은 곳: https://www.mt.co.kr/industry/2024/11/02/2024110115033217577 (안좌 288MW, 임자 99.9MW, 비금 200MW)
  #          https://www.etoday.co.kr/news/view/2100361 (지도·사옥도 150MW, 2022-01-26 상업운전)
  - id: solar_sinan_anjwa  # 신안 안좌
    fuel: solar
    p_max_mw: 288
    p_min_mw: 0
    ramp_up_mw_per_h: 0
    ramp_down_mw_per_h: 0
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0.054
    u_init: 0
    init_state_h: 0
  - id: solar_sinan_bigeum  # 신안 비금 (2024-11 기준 준공 직전)
    fuel: solar
    p_max_mw: 200
    p_min_mw: 0
    ramp_up_mw_per_h: 0
    ramp_down_mw_per_h: 0
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0.054
    u_init: 0
    init_state_h: 0
  - id: solar_sinan_jido  # 신안 지도·사옥도
    fuel: solar
    p_max_mw: 150
    p_min_mw: 0
    ramp_up_mw_per_h: 0
    ramp_down_mw_per_h: 0
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0.054
    u_init: 0
    init_state_h: 0
  - id: solar_sinan_imja  # 신안 임자
    fuel: solar
    p_max_mw: 99.9
    p_min_mw: 0
    ramp_up_mw_per_h: 0
    ramp_down_mw_per_h: 0
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0.054
    u_init: 0
    init_state_h: 0
  - id: solar_solaseado  # 솔라시도 (미확인)
    fuel: solar
    p_max_mw: 98
    p_min_mw: 0
    ramp_up_mw_per_h: 0
    ramp_down_mw_per_h: 0
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0.054
    u_init: 0
    init_state_h: 0
  # ---- 풍력 (단지 기준, 기동·정지 항목은 없음) ----
  # 찾은 곳: https://www.smarttoday.co.kr/ko-kr/articles/98675 (낙월해상 364.8MW)
  #          https://www.mt.co.kr/economy/2019/04/04/2019040414064544327 (영광풍력 79.6MW = 육상 45.1 + 해상 34.5)
  - id: wind_nakwol_offshore  # 낙월해상 (일부만 상업운전 중)
    fuel: wind
    p_max_mw: 364.8
    p_min_mw: 0
    ramp_up_mw_per_h: 0
    ramp_down_mw_per_h: 0
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0
    u_init: 0
    init_state_h: 0
  - id: wind_jeju_hallim_offshore  # 제주한림해상 (미확인)
    fuel: wind
    p_max_mw: 100
    p_min_mw: 0
    ramp_up_mw_per_h: 0
    ramp_down_mw_per_h: 0
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0
    u_init: 0
    init_state_h: 0
  - id: wind_jeonnam_offshore  # 전남해상 (약 96~99MW, 미확인)
    fuel: wind
    p_max_mw: 96
    p_min_mw: 0
    ramp_up_mw_per_h: 0
    ramp_down_mw_per_h: 0
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0
    u_init: 0
    init_state_h: 0
  - id: wind_gangwon  # 강원풍력 (미확인)
    fuel: wind
    p_max_mw: 98
    p_min_mw: 0
    ramp_up_mw_per_h: 0
    ramp_down_mw_per_h: 0
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0
    u_init: 0
    init_state_h: 0
  - id: wind_yeonggwang  # 영광풍력
    fuel: wind
    p_max_mw: 79.6
    p_min_mw: 0
    ramp_up_mw_per_h: 0
    ramp_down_mw_per_h: 0
    min_up_h: 0
    min_down_h: 0
    startup_cost_krw: 0
    co2_ton_per_mwh: 0
    u_init: 0
    init_state_h: 0

[ 내려받은 자료 ] (2026-10-05, docs/pdf 에 저장)
- docs/pdf/KPX_계통신뢰도를_고려한_양수발전기_운영방안_연구_201303.pdf (254쪽)
  https://new.kpx.or.kr/boardDownload.es?bid=0045&list_no=51427&seq=16063
  쓰인 곳: 양수 ramp (표 2-3), 기동 3~5분 (표 2-2), 호기 구성 (표 2-4)
- docs/pdf/기후솔루션_화력발전기_최소발전용량.pdf (21쪽)
  https://content.forourclimate.org/files/research/BSjRQ8e.pdf
  쓰인 곳: 유연탄 p_min 60%, 가스 p_min 48% (표2)
- docs/pdf/윤미향의원_2023_국정감사_정책보고서_대한민국_산림의_땔감화.pdf (2쪽)
  https://www.kpx.or.kr/boardDownload.es?bid=0048&list_no=72212OOO202404111334002042&seq=2
  쓰인 곳: 신재생(바이오매스) 설비용량
  - 주의: 앞에서 "전력거래소 [표 5]" 로 적었으나 전력거래소 문서가 아니라 전력거래소 사이트에 올라온 국정감사 정책보고서 (원 파일명으로 확인, 위 표기 고침)
- docs/pdf/KPX_발전기_기동비용_산정_및_적용기준_개선방안_연구_201210.pdf (142쪽)
  https://www.kpx.or.kr/boardDownload.es?bid=0045&list_no=51351&seq=16281
  전체를 다시 확인함: 호기별 기동비용 값은 없음 (표-15, 16 은 계산 방법을 보여주는 예시 수치). 값의 출처로는 쓰이지 않음
- docs/pdf/기후솔루션_제주_출력제어_최소발전용량_하향_편익.pdf
  https://content.forourclimate.org/files/research/aunioBf.pdf
  쓰인 곳: 위 [찾은 곳] 의 제주 발전기 최소발전용량 표 (YAML 값에는 쓰이지 않음)
- docs/pdf/KNS_APR1400_탄력운전_2026춘계_민지홍.pdf
  https://www.kns.org/boards/download/33888
  원문 확인: APR1400 탄력운전 적용 - 최소 부하 50%, 최대 출력 변동률 시간당 30%, 연간 200회
  (앞에서 "검색 결과 문구만 확인" 이라고 적었던 30%/h 가 원문으로 확인됨. YAML 의 원자력 값은 현행 운전 기준 1,120 / 42 그대로 둠)
- docs/web/Kwater_한강유역본부_시설현황.html
  https://www.kwater.or.kr/busi/sub02/facilitiespresentPage.do?s_mid=1511
  쓰인 곳: 수력 p_max (충주 10만kW급 4기, 소양강 10만kW x 2대)
- 나머지 출처는 기사·웹페이지라 파일 없이 URL 만 있음


[ 배율 설정 ]
발전원별 data-source 합 / EPSIS 설비용량 합 / 비율 (2026-10-06)
- data-source 합: 위 generators 42기의 p_max_mw 합 (MW)
- EPSIS 설비용량 합: data/csv/연료원별_설비용량_utf8_20250701_20260801.csv 의 2026/08 행 (MW)
- 비율 = EPSIS 설비용량 합 / data-source 합 (배)

  fuel             data-source 합  EPSIS 설비용량 합      비율   발전원
  nuclear          7,000.0         26,050.0           3.72   원자력
  bituminous_coal  5,240.0         40,366.7           7.70   유연탄
  lng              5,079.0         46,788.3           9.21   LNG
  anthracite       400.0           400.0              1.00   국내탄 (EPSIS 무연탄)
  pumped           1,800.0         4,700.0            2.61   양수
  hydro            500.0           1,815.7            3.63   수력
  renewable        1,059.0         3,962.3            3.74   신재생 (EPSIS 연료전지1,518.0+석탄가스화346.3+해양254.6+바이오1,843.4+폐기물0)
  solar            835.9           33,059.5           39.55  태양광 (EPSIS 태양)
  wind             738.4           2,569.5            3.48   풍력
  (없음)            0               627.0              -      data-source 에 없는 것(유류161.9+기타465.1)
  total            22,652.3        160,411.1          7.08

1. 설비 용량이 매달 늘어나고 있음 (특히 태양력 발전) -> 이걸 고정값으로 할지 변동값으로 할지
2. 적용 비율이 변경되고 있음 (풍력 12, 태양력 13) -> 위에랑 비슷한 문제임
3. 비율을 어떡게 적용할까? :: 생성량, 설비 용량 * 비율 or 가상 발전기 개수 늘리기

**A. 용량·발전량 × 비율 (발전기 42기 유지, 크기만 키움)**

장점
- 이진변수 수가 그대로(42기 × 시간)라 풀이 시간이 늘지 않습니다. 10초 제한과 168h·Stochastic 확장에 유리합니다.
- 3.72, 9.21 같은 비정수 비율을 그대로 쓸 수 있습니다.
- 비율이 달마다 바뀌어도(1·2번 문제) 파라미터 값만 바꾸면 되고 모델 구조는 그대로입니다.
- 구현이 단순하고 `generators.yaml`도 그대로입니다.

단점
- 발전기 1기가 비현실적으로 커집니다(원전 1,400MW → 약 5,200MW, LNG는 9배).
- 기동·정지 단위가 거칠어져, p_min도 같이 커지므로 저수요 시간대에 과잉 발전이나 infeasible이 날 수 있습니다.
- p_max뿐 아니라 p_min, 램프율, 기동비용도 같은 비율로 곱해야 하며, 하나라도 빠지면 비용이 왜곡됩니다.
- "1,400MW 탈락" 시나리오가 발전기 1기와 대응되지 않아 별도 처리가 필요합니다.

**B. 가상 발전기 개수 늘리기 (크기 유지, 복제)**

장점
- 발전기 크기와 파라미터(p_min, 램프율, 기동비용, 최소 기동·정지 시간)를 원본 그대로 쓰므로 UC 결과가 현실적입니다.
- 기동·정지가 세밀해져 수요 추종이 자연스럽고, Rule-based 대비 절감률도 더 설득력 있게 나옵니다.
- 1,400MW 탈락이 "원전 1기 off"로 바로 표현됩니다.

단점
- 발전기가 약 7배(42기 → 300기 안팎)가 되어 이진변수도 그만큼 늘고, 10초 제한을 넘길 위험이 큽니다.
- 동일한 복제 발전기끼리 대칭성이 생겨 branch-and-bound가 느려집니다.
- 개수는 정수여야 하므로 3.72배는 4기로 반올림하게 되어 EPSIS 합계와 오차가 생깁니다.
- 비율이 바뀌면 발전기 개수, 즉 모델 구조가 바뀌어 1·2번 문제에 대응하기 어렵습니다.
