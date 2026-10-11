"""dispatch_api 공용 상수.

연료 코드는 collector 의 services/collector/domain.py FUELS 와 값이 일치해야 한다
(fuel_cost 데이터셋 조인 키). CLAUDE.md 필수 규칙상 다른 서비스의 내부 Python
모듈을 import 할 수 없으므로 값으로 복제한다. collector 쪽 FUELS 가 바뀌면 여기도
같이 바꿔야 한다.
"""

from __future__ import annotations

# ── 단위 변환 ────────────────────────────────────────────────────────────────
# 프로젝트 단위 규칙: 전력 MW, 에너지 MWh, 비용 원/MWh, 배출 tCO2/MWh.
# 이 상수들을 쓰는 지점은 로더 한 곳이어야 한다 (변환이 흩어지면 1000배 버그).
KRW_PER_KWH_TO_KRW_PER_MWH = 1000
G_CO2_PER_KWH_TO_TON_PER_MWH = 0.001

MINUTES_PER_HOUR = 60

# ── 연료 코드 (collector domain.py FUELS 10종) ───────────────────────────────
FUEL_CODES: tuple[str, ...] = (
    "hydro",
    "oil",
    "bituminous_coal",
    "nuclear",
    "pumped",
    "lng",
    "anthracite",
    "renewable",
    "solar",
    "wind",
)

# 급전 가능 — generators.yaml 에 유닛으로 들어오는 발전원.
DISPATCHABLE_FUELS: tuple[str, ...] = (
    "nuclear",
    "bituminous_coal",
    "anthracite",
    "lng",
    "oil",
    "hydro",
)

# 비급전(must-take) — 예측 시계열로 들어와 순수요에서 차감된다.
# wind 는 collector 가 trading_by_fuel 로 바꾸면서 신설됐다. 그 전에는 풍력이
# renewable(신재생)에 묶여 있었다. 지금은 분리돼 있으므로 둘 다 차감한다.
MUST_TAKE_FUELS: tuple[str, ...] = ("solar", "wind", "renewable")

# 저장장치 성격 — ESS 와 같이 다루며 generators.yaml 에서 제외한다.
STORAGE_FUELS: tuple[str, ...] = ("pumped",)

# ── 결측 표기 (collector domain.py MISSING / docs/api_spec.md 결측치 규약) ────
# data API 는 값이 없는 칸을 문자열 "None" 으로 내보낸다. 수집 데이터에서 0 은
# 실제 값 0 이므로, "행은 있는데 값이 없음"을 0 으로 읽으면 안 된다.
# 반대로 generators.yaml 의 0 은 override 값이고 MILP 계수라 실제 숫자여야 한다.
# 두 규약이 만나는 지점은 로더뿐이므로 여기서만 해석한다.
MISSING_MARKERS: frozenset[str] = frozenset({"None", "null", ""})

# ── 시계열 포맷 (collector domain.py TS_FORMAT / MONTH_FORMAT) ───────────────
TS_FORMAT = "%Y-%m-%d %H:%M:%S"
MONTH_FORMAT = "%Y-%m"

# CLAUDE.md: timestamp 는 timezone-aware (Asia/Seoul) 로 다룬다.
DEFAULT_TIMEZONE = "Asia/Seoul"
