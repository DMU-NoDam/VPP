"""dashboard — 통합 관제 대시보드 (Streamlit :8501).

화면만 그린다. 설정 로드·REST 호출·계산은 logic.py 에 있고, 판정 기준과 목업 설비값은
config/dashboard.yaml 에 있다.

페이지 구성:
    통합 관제      스크롤 없이 한 화면 — 수급 게이지·알람, Merit Order, 최적화 결과,
                   탄소 추이, ESS, 위기 시나리오 (CLAUDE.md 최종 결과물 4·기능 요구 10)
    분석 상세      168h 예측·성능 지표, 최적화 상세, 위기 시나리오 게이지, 배출 내역
    시간대 데이터  KPX 실측, 출력 히트맵, 시간대 상세표·CSV
    시나리오 생성기 page_scenarios.py
    Two-Stage 테스트 page_stochastic_test.py — RP·EV·EEV·WS 비교, VSS 판정
사이드바의 관측 시각·조정값은 앞의 세 페이지가 함께 쓴다 (main 에서 한 번 그린다).

데이터 출처 (모두 REST, 다른 서비스 코드는 import 하지 않는다):
    collector     GET  /data/{name}                    실측 수요·발전원별 출력·SMP
                                                       (수요 게이지, Merit Order 실측 발전 믹스)
    forecast_api  GET  /health                         연결 상태
                  POST /api/v1/forecast                168h 수요 예측·90% PI (분석 상세)
    dispatch_api  GET  /health                         연결 상태
                  POST /api/v1/dispatch/milp           MILP 절감률·비용·제약 (cost 모드),
                                                       ESS 충방전·피크 감소 (peak_shaving 모드),
                                                       탄소 ESS 전/후·Rule-based 대비 (use_ess 끔)
                  GET  /api/v1/dispatch/stochastic/latest  VSS (미리 계산, StochasticLatest)
                  POST /api/v1/scenarios               시나리오 생성기 페이지 (page_scenarios.py)
응답이 없거나 계약이 아직 없는 패널은 목업으로 그린다. 서비스 응답을 쓴 패널에는 LIVE,
목업으로 그린 패널에는 목업 태그를 단다 (ui.source_tag, ui.mock_tag).

TODO(통합): 수급 게이지의 공급능력·예비율, 위기 시나리오는 아직 목업이다. MILP 계획은
수집 데이터의 최신 연속 24h(관측 시각과 다른 날일 수 있음) 기준이라 실시간 게이지에 바로
쓰지 않았다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
from plotly.subplots import make_subplots
from streamlit.delta_generator import DeltaGenerator

import logic
import page_scenarios
import page_stochastic_test
from logic import API_PREFIX, ApiResult, Settings
from ui import (
    C_BG,
    C_BLUE,
    C_BRIGHT,
    C_GREEN,
    C_INDIGO,
    C_ORANGE,
    C_RED,
    C_TEXT,
    C_YELLOW,
    CFG,
    HEAT_SCALE,
    badge,
    compact,
    fetch_health,
    gauge_label,
    get_settings,
    inject_custom_css,
    kpi_badge,
    mock_tag,
    panel_title,
    pw,
    source_tag,
    style_dark,
    ton,
    won,
)

# 발전원 코드 → 색 (logic 목업 설비와 같은 코드)
FUEL_COLOR = {
    "nuclear": "#a78bfa",
    "hydro": "#3b82f6",
    "solar": "#facc15",
    "wind": "#2dd4bf",
    "coal": "#64748b",
    "lng": "#fb923c",
}
# collector trading_by_fuel 의 발전원 코드 → 표시 이름
COLLECTOR_FUEL_KO = {
    "nuclear": "원자력", "bituminous_coal": "유연탄", "anthracite": "국내탄", "lng": "가스",
    "oil": "유류", "hydro": "수력", "pumped": "양수", "solar": "태양광", "wind": "풍력",
    "renewable": "신재생",
}
# collector trading_by_fuel 발전원 색 (실측 발전 믹스). 6종과 겹치는 건 FUEL_COLOR 와 같게
COLLECTOR_FUEL_COLOR = {
    "nuclear": FUEL_COLOR["nuclear"], "bituminous_coal": FUEL_COLOR["coal"],
    "anthracite": "#475569", "lng": FUEL_COLOR["lng"], "oil": "#f472b6",
    "hydro": FUEL_COLOR["hydro"], "pumped": "#93c5fd", "solar": FUEL_COLOR["solar"],
    "wind": FUEL_COLOR["wind"], "renewable": "#5eead4",
}
# MILP 계획 스택에만 있는 항목 (logic.milp_fuel_stack)
PLAN_EXTRA_KO = {"ess_discharge": "ESS 방전", "ess_charge": "ESS 충전", "unknown": "기타"}
PLAN_EXTRA_COLOR = {"ess_discharge": "#e879f9", "ess_charge": "#a21caf", "unknown": C_TEXT}
# 실측 발전원 → 아이콘 (gen_icon 은 6종만 있다)
REAL_ICON = {
    "nuclear": "nuclear", "bituminous_coal": "coal", "anthracite": "coal", "lng": "lng",
    "hydro": "hydro", "solar": "solar", "wind": "wind",
}
STATUS_COLOR = {"ok": C_GREEN, "warn": C_YELLOW, "critical": C_RED}
# collector 수집 주기 (services/collector/main.py INTERVAL_SEC=300). 바꾸면 같이 바꾼다.
COLLECTOR_INTERVAL_MIN = 5
# 한 화면 배치를 위한 차트 높이 (1080p 기준 스크롤 없음)
H_MERIT, H_SMALL = 372, 232
NO_BAR = {"displayModeBar": False}


# 발전원 아이콘 (인라인 SVG — 외부 이미지 파일 의존 없음)
_ICON_SVG = {
    "nuclear": (
        '<circle cx="12" cy="12" r="2.1" fill="{c}" stroke="none"/>'
        '<ellipse cx="12" cy="12" rx="9.6" ry="4.1"/>'
        '<ellipse cx="12" cy="12" rx="9.6" ry="4.1" transform="rotate(60 12 12)"/>'
        '<ellipse cx="12" cy="12" rx="9.6" ry="4.1" transform="rotate(120 12 12)"/>'
    ),
    "hydro": (
        '<path d="M12 2.8s-6.6 7.3-6.6 11.3a6.6 6.6 0 0 0 13.2 0C18.6 10.1 12 2.8 12 2.8Z"/>'
        '<path d="M8.6 14.6c1.1-1 2.3-1 3.4 0s2.3 1 3.4 0"/>'
    ),
    "solar": (
        '<circle cx="12" cy="12" r="4"/>'
        '<path d="M12 1.8v2.6M12 19.6v2.6M1.8 12h2.6M19.6 12h2.6'
        'M4.8 4.8l1.9 1.9M17.3 17.3l1.9 1.9M19.2 4.8l-1.9 1.9M6.7 17.3l-1.9 1.9"/>'
    ),
    "wind": (
        '<path d="M12 13.1V21.6M9.4 21.6h5.2"/>'
        '<circle cx="12" cy="11.6" r="1.5" fill="{c}" stroke="none"/>'
        '<path d="M12 10.1V2.6M13.4 12.4l6.4 3.7M10.6 12.4l-6.4 3.7"/>'
    ),
    "coal": (
        '<path d="M2.6 20.6v-8.2l5.4 3.1v-3.1l5.4 3.1v-3.1l5.4 3.1v5.1Z"/>'
        '<path d="M6.2 9.2V5.6h2.8v4.9"/>'
    ),
    "lng": (
        '<path d="M12 21.4c3.5 0 6.1-2.4 6.1-5.7 0-3.5-2.6-5.3-3.8-8.2-.6 2.1-1.7 2.9-2.7 3.7'
        ".2-2.6-.8-5.3-2.6-7.4-.4 2.9-2.3 4.3-3.5 6.4-.8 1.5-1.2 3.1-1.2 4.9 0 3.5 2.7 6.3 "
        '7.7 6.3Z"/>'
    ),
}


@dataclass
class Context:
    """한 번의 실행에서 여러 페이지가 함께 쓰는 값 (사이드바 조정 반영)."""

    settings: Settings
    urls: dict[str, str]
    health: dict[str, ApiResult]
    hour: int
    adjust: logic.Adjust
    base: pd.DataFrame      # 오늘 기본값 시계열 (조정 비교용)
    df: pd.DataFrame        # 오늘 조정 반영 시계열
    now: pd.Series          # 관측 시각 행
    period: pd.DataFrame    # 분석 기간 시계열 (탄소·ESS, 조정 반영)
    start: date
    end: date
    real: RealMix | None = None  # collector 실측 하루 (발전 믹스·탄소·이용률). 없으면 목업

    @property
    def prev(self) -> pd.Series:
        """관측 시각 한 시간 전 행 (0시면 23시)."""
        return self.df.loc[self.df.hour == (self.hour - 1) % 24].iloc[0]

    @property
    def days(self) -> int:
        """분석 기간 일수."""
        return (self.end - self.start).days + 1

    @property
    def period_label(self) -> str:
        """분석 기간 표시 (예: 09/25–10/01 · 7일)."""
        if self.days == 1:
            return f"{self.start:%m/%d} 하루"
        return f"{self.start:%m/%d}–{self.end:%m/%d} · {self.days}일"


# ── 데이터 가져오기 (캐시) ───────────────────────────────────────────────────


@st.cache_data(ttl=CFG.cache_ttl_s, show_spinner=False)
def fetch_collector(base_url: str, name: str, limit: int) -> ApiResult:
    """collector 데이터셋의 최근 limit 행."""
    return logic.call_api("GET", f"{base_url}/data/{name}", params={"limit": limit},
                          timeout=CFG.api.timeout_short_s)


@st.cache_data(ttl=CFG.cache_ttl_s, show_spinner=False)
def fetch_stochastic_latest(base_url: str) -> ApiResult:
    """GET /dispatch/stochastic/latest. dispatch_api 가 미리 계산해 둔 결과라 바로 온다."""
    return logic.call_api("GET", f"{base_url}{API_PREFIX}/dispatch/stochastic/latest",
                          timeout=CFG.api.timeout_short_s)


@st.cache_data(ttl=CFG.api.ttl_range_s, show_spinner=False)
def fetch_collector_range(base_url: str, name: str, start: str, end: str) -> ApiResult:
    """collector 데이터셋의 start~end 구간 (시간 컬럼 기준, 양 끝 포함)."""
    return logic.call_api("GET", f"{base_url}/data/{name}",
                          params={"start": start, "end": end}, timeout=CFG.api.timeout_short_s)


@dataclass
class RealMix:
    """실측 발전 믹스 하루치 (Merit Order 패널)."""

    day: date
    mix: pd.DataFrame       # hour, fuel, mw, demand_mw (logic.real_mix)
    order: list[str]        # 연료비 순 발전원 코드
    co2: dict[str, float]   # 연료별 배출계수 (generators.yaml)
    latency_s: float | None

    @property
    def tag(self) -> str:
        """패널 제목 옆 실측 태그."""
        return real_tag(f"실측 {self.day:%m/%d}")


def real_mix(collector_url: str) -> RealMix | None:
    """collector 실측으로 가장 최근 하루치 발전 믹스와 같은 날 수요. 없으면 None.

    trading_by_fuel 은 10개 발전원 x 시간이라 최근 5일치(1200행)에서 완전한 날을 찾는다.
    """
    trading_res = fetch_collector(collector_url, "trading_by_fuel", 1200)
    trading = logic.rows_frame(trading_res, ("trade_mwh",))
    day = logic.latest_full_day(trading)
    if day is None:
        return None
    demand = logic.rows_frame(
        fetch_collector_range(collector_url, "power_demand", f"{day} 00:00:00", f"{day} 23:59:59"),
        ("demand_mw",),
    )
    fuel_cost = logic.rows_frame(fetch_collector(collector_url, "fuel_cost", 60),
                                 ("cost_won_per_kwh",))
    mix = logic.real_mix(trading, demand, day)
    order = logic.merit_fuel_order(sorted(mix.fuel.unique()), fuel_cost)
    return RealMix(day=day, mix=mix, order=order, co2=fuel_co2(),
                   latency_s=trading_res.latency_s)


@st.cache_data(show_spinner=False)
def fuel_co2() -> dict[str, float]:
    """연료별 배출계수 (config/generators.yaml, C 담당)."""
    return logic.load_fuel_co2()


def real_tag(text: str) -> str:
    """실측 태그 (LIVE 와 같은 모양, 기준 시각을 함께 쓴다)."""
    return f'<span class="vpp-src live">{text}</span>'


@st.cache_data(ttl=CFG.api.ttl_range_s, show_spinner="수요 예측 호출 중…")
def fetch_forecast(base_url: str, timeout: float) -> ApiResult:
    """POST /forecast (수요·태양광·풍력). 기간·horizon 은 config 의 forecast_request."""
    today = date.today()
    payload = {
        "start_date": (today - timedelta(days=CFG.forecast_history_days)).isoformat(),
        "end_date": today.isoformat(),
        "target": "all",
        "horizon_hours": CFG.forecast_horizon_h,
    }
    return logic.call_api("POST", f"{base_url}{API_PREFIX}/forecast", payload=payload,
                          timeout=timeout)


def forecast_result(ctx: Context) -> tuple[pd.DataFrame, dict, bool, float | None]:
    """(예측 시계열, 지표, LIVE 여부, 지연시간). 서비스가 없거나 응답이 깨지면 목업."""
    res = (
        fetch_forecast(ctx.urls["forecast_api"], ctx.settings.timeout_s)
        if ctx.health["forecast_api"].ok
        else ApiResult(error="forecast_api down")
    )
    try:
        if res.ok:
            fc, metrics = logic.parse_forecast(res.data)
            return fc, metrics, True, res.latency_s
    except (KeyError, TypeError, ValueError, IndexError):
        pass
    fc = logic.sample_forecast(ctx.settings)
    return fc, logic.forecast_metrics(fc, ctx.settings), False, None


def stochastic_result(ctx: Context, label: str = "") -> tuple[dict, str]:
    """(VSS 요약, 출처 태그). dispatch_api 의 미리 계산 결과를 쓰고, 없으면 목업.

    결과에는 계산 조건(seed·예비율·계산 시각)이 같이 들어 있다 (logic.parse_stochastic_latest).
    """
    prefix = f"{label} " if label else ""
    res = (
        fetch_stochastic_latest(ctx.urls["dispatch_api"])
        if ctx.health["dispatch_api"].ok
        else ApiResult(error="dispatch_api down")
    )
    parsed = None
    try:
        if res.ok:
            parsed = logic.parse_stochastic_latest(res.data)
    except (KeyError, TypeError, ValueError):
        parsed = None
    if parsed is not None:
        at = parsed["computed_at"]
        return parsed, real_tag(f"{prefix}계산 {at:%m/%d %H:%M}" if at else f"{prefix}LIVE")
    mock = logic.parse_stochastic(logic.sample_stochastic())
    if res.ok and res.data.get("state") in ("pending", "running"):
        return mock, mock_tag(f"{prefix}계산 중 · 목업")
    return mock, mock_tag(f"{prefix}목업")


@st.cache_data(ttl=CFG.api.ttl_range_s, show_spinner="MILP 급전 계획 계산 중…")
def fetch_milp(base_url: str, ess_mode: str | None, timeout: float,
               use_ess: bool = True, include_units: bool = False) -> ApiResult:
    """POST /dispatch/milp. 수요를 주지 않으면 dispatch_api 가 수집 데이터의 최신 연속 24h 를 쓴다.

    ess_mode None 은 config/ess.yaml 기본(cost). 유닛 x 시간 급전값(응답 ~70KB)은 Merit Order
    스택이 쓰는 기본 계획에서만 받는다.
    """
    payload: dict = {"include_units": include_units}
    if ess_mode:
        payload["ess_mode"] = ess_mode
    if not use_ess:
        payload["use_ess"] = False
    return logic.call_api("POST", f"{base_url}{API_PREFIX}/dispatch/milp", payload=payload,
                          timeout=timeout)


def milp_result(ctx: Context, ess_mode: str | None = None, use_ess: bool = True) -> dict | None:
    """MILP 요약 (logic.parse_milp). 서비스가 없거나 해가 없으면 None — 호출한 쪽이 목업을 쓴다.

    비용 비교는 기본 모드(cost), ESS 피크 감소 KPI 는 peak_shaving 으로 잰다
    (formulation.md 5.3: cost 모드는 피크 감소를 보장하지 않는다). use_ess=False 는 탄소의
    ESS 전/후 비교용이다.
    """
    if not ctx.health["dispatch_api"].ok:
        return None
    # 기본 계획(cost 모드 + ESS)만 유닛 출력을 받는다 — 같은 인자라야 캐시를 같이 쓴다
    units = ess_mode is None and use_ess
    res = fetch_milp(ctx.urls["dispatch_api"], ess_mode, ctx.settings.timeout_s, use_ess, units)
    try:
        return logic.parse_milp(res.data, res.latency_s) if res.ok else None
    except (KeyError, TypeError, ValueError):
        return None


def milp_carbon_pair(ctx: Context) -> tuple[dict, dict] | None:
    """(MILP+ESS, MILP ESS 없음) — 같은 날 cost 모드 두 번. 하나라도 없으면 None."""
    with_ess = milp_result(ctx)
    no_ess = milp_result(ctx, use_ess=False) if with_ess else None
    return (with_ess, no_ess) if with_ess and no_ess else None


def milp_tag(milp: dict, label: str = "MILP") -> str:
    """MILP 계획 태그. 계획 날짜(수집 데이터의 최신 연속 24h)를 같이 쓴다."""
    return real_tag(f"{label} {milp['day']:%m/%d} 계획")


# ── 작은 그리기 도구 ─────────────────────────────────────────────────────────


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """'#rrggbb' → (r, g, b)."""
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def gen_icon(code: str, size: int = 24) -> str:
    """발전원 아이콘 SVG를 해당 발전원 색상으로 렌더한다."""
    color = FUEL_COLOR[code]
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" '
        f'style="flex:none">'
        f"{_ICON_SVG[code].format(c=color)}</svg>"
    )


def html(text: str) -> None:
    """HTML 조각 출력."""
    st.markdown(text, unsafe_allow_html=True)


def _pct_change(a: float, b: float) -> float:
    """b 대비 a 의 변화율 (%)."""
    return (a - b) / b * 100 if b else 0.0


def _delta(value: float, unit: str = "%") -> str:
    """전 시간 대비 변화 HTML (↑ 초록 / ↓ 빨강)."""
    direction = "up" if value >= 0 else "down"
    return f'<span class="vpp-kpi-delta {direction}">{abs(value):.1f}{unit}</span>'


def _gauge_figure(
    value: float, value_max: float, suffix: str = "",
    steps: list[dict] | None = None, value_min: float = 0.0,
    show_number: bool = True, height: int = 215, bar_color: str = C_BLUE,
) -> go.Figure:
    """게이지 도형만 그린다.

    라벨을 plotly title로 넣으면 패널 폭이 넓어질 때(사이드바 접힘 등) 아크가
    커지면서 상단 여백 밖으로 밀려 잘린다. 라벨은 HTML 로 따로 붙인다.
    """
    fig = go.Figure(
        go.Indicator(
            mode="gauge+number" if show_number else "gauge",
            value=value,
            number={"suffix": suffix, "font": {"color": C_BRIGHT, "size": 26}},
            gauge={
                "axis": {
                    "range": [value_min, value_max],
                    "tickvals": [value_min, value_max],
                    "showticklabels": show_number,  # 작은 게이지는 폭이 좁아 눈금 숫자가 잘린다
                    "tickcolor": "rgba(0,0,0,0)",
                    "tickfont": {"size": 11, "color": C_TEXT},
                },
                "bar": {"color": bar_color, "thickness": 0.42},
                "bgcolor": "rgba(255,255,255,.06)",
                "borderwidth": 0,
                "steps": steps or [],
            },
        )
    )
    fig = style_dark(fig, height=height)
    fig.update_layout(margin=dict(l=24, r=24, t=10, b=4))
    return fig


def _reserve_steps(settings: Settings, value_min: float = 0.0) -> list[dict]:
    """예비율 게이지 색 구간 (Critical / Warning / Normal)."""
    crit, warn = settings.reserve_critical_pct, settings.reserve_warning_pct
    return [
        {"range": [value_min, crit], "color": "rgba(248,113,113,.30)"},
        {"range": [crit, warn], "color": "rgba(250,204,21,.28)"},
        {"range": [warn, 30], "color": "rgba(34,197,94,.16)"},
    ]


def _melt_by_fuel(settings: Settings, df: pd.DataFrame) -> pd.DataFrame:
    """시간 x 발전원 긴 표 (hour, 발전원, mw)."""
    names = {f"{f.code}_mw": f.name for f in settings.fleet}
    melted = df.melt(id_vars=["hour"], value_vars=list(names), var_name="g", value_name="mw")
    melted["발전원"] = melted["g"].map(names)
    return melted


def _legend_right(fig: go.Figure) -> go.Figure:
    """범례를 차트 오른쪽에 세로로 (항목이 많은 Merit Order 용). 위 → 아래 = 연료비 낮은 순."""
    fig.update_layout(
        legend=dict(orientation="v", x=1.01, xanchor="left", y=1, yanchor="top",
                    font=dict(size=12), traceorder="normal"),
        margin=dict(l=4, r=4, t=8, b=4),
    )
    return fig


def _compact_chart(fig: go.Figure, height: int) -> go.Figure:
    """한 화면용 작은 차트: 범례는 위 오른쪽, 축 제목 없음, 여백 최소."""
    fig = style_dark(fig, height=height)
    fig.update_layout(
        margin=dict(l=4, r=4, t=22, b=4),
        legend=dict(orientation="h", x=1, xanchor="right", y=1.12, font=dict(size=12)),
    )
    fig.update_xaxes(title=None, tickfont=dict(size=11))
    fig.update_yaxes(title=None, tickfont=dict(size=11))
    return fig


# ── 사이드바 (모든 관제 페이지 공용) ─────────────────────────────────────────


def _adjust_defaults(settings: Settings) -> dict[str, float]:
    """조정 위젯 key → 기본값. 되돌리기 버튼이 이 값으로 되돌린다."""
    ess = settings.ess
    return {
        "adj_demand": 100, "adj_solar": 100, "adj_wind": 100, "adj_outage": 0,
        "adj_ess_power": int(ess.power_mw), "adj_ess_energy": int(ess.energy_mwh),
        "adj_ess_rte": int(round(ess.round_trip_efficiency * 100)),
    }


def _changed_adjust(settings: Settings) -> list[str]:
    """기본값과 다른 조정 위젯 key 목록."""
    defaults = _adjust_defaults(settings)
    return [k for k, v in defaults.items() if st.session_state.get(k, v) != v]


def _reset_adjust(defaults: dict[str, float]) -> None:
    """되돌리기 버튼 콜백."""
    for key, value in defaults.items():
        st.session_state[key] = value


def _heat_demand_pct(settings: Settings) -> float:
    """위기 시나리오 중 수요를 키우는 것(폭염)의 수요 배율 (%)."""
    return next(sc.demand_mult for sc in settings.crisis if sc.demand_mult != 1.0) * 100


def render_adjust_controls(settings: Settings) -> logic.Adjust:
    """사이드바 what-if 조정 위젯. 모든 패널이 이 값으로 다시 계산된다."""
    defaults = _adjust_defaults(settings)
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)

    panel_title("시뮬레이션 조정")
    st.slider("수요 (%)", 80, 120, step=1, key="adj_demand",
              help=f"시간대별 수요 전체에 곱한다. 폭염 시나리오는 {_heat_demand_pct(settings):g}%.")
    st.slider("태양광 출력 (%)", 0, 150, step=5, key="adj_solar")
    st.slider("풍력 출력 (%)", 0, 150, step=5, key="adj_wind")
    st.slider("원자력 탈락 (MW)", 0, 6000, step=200, key="adj_outage",
              help="원자력 설비에서 빼고, 빠진 만큼 석탄·LNG가 대신 급전한다.")
    with st.expander("ESS 사양", expanded=False):
        st.slider("출력 (MW)", 0, 10000, step=250, key="adj_ess_power")
        st.slider("용량 (MWh)", 0, 40000, step=1000, key="adj_ess_energy")
        st.slider("왕복효율 (%)", 60, 100, step=1, key="adj_ess_rte")

    changed = _changed_adjust(settings)
    st.button(
        f"기본값으로 되돌리기 ({len(changed)}개 변경)" if changed else "기본값 그대로",
        on_click=_reset_adjust, args=(defaults,), disabled=not changed, width="stretch",
    )

    ss = st.session_state
    return logic.Adjust(
        demand_pct=ss.adj_demand, solar_pct=ss.adj_solar, wind_pct=ss.adj_wind,
        outage_mw=ss.adj_outage, ess_power_mw=ss.adj_ess_power,
        ess_energy_mwh=ss.adj_ess_energy, ess_rte=ss.adj_ess_rte / 100,
    )


def render_period_input() -> tuple[date, date]:
    """탄소·ESS 분석 기간 (최종 결과물 3: 대시보드에서 분석 기간 선택)."""
    today = date.today()
    picked = st.date_input(
        "분석 기간 (탄소 · ESS)", value=(today, today), key="period",
        min_value=today - timedelta(days=365), max_value=today, format="YYYY-MM-DD",
        help=f"시작·끝 날짜를 차례로 고른다. 최대 {logic.MAX_PERIOD_DAYS}일 "
             "(넘으면 끝 날짜 기준으로 자른다).",
    )
    # 범위를 고르는 도중에는 날짜가 하나만 온다
    days = list(picked) if isinstance(picked, (tuple, list)) else [picked]
    if not days:
        return today, today
    return logic.clamp_period(days[0], days[-1])


def render_sidebar(
    settings: Settings, health: dict[str, ApiResult]
) -> tuple[int, tuple[date, date], logic.Adjust]:
    """관측 시각·분석 기간·보기 설정·조정 위젯·서비스 상태. (시각, 기간, 조정값)을 돌려준다."""
    with st.sidebar:
        panel_title("관측 설정")
        hour = st.slider("관측 시각 (시)", 0, 23, 19)
        period = render_period_input()
        st.toggle("큰 단위로 보기 (GW · 억원 · 천t)", value=True, key="compact_units")

        adjust = render_adjust_controls(settings)

        panel_title("서비스 연결 (REST /health)")
        html("".join(
            badge(f"{name} · {res.latency_s:.2f}s" if res.ok else f"{name} · {res.error}",
                  "ok" if res.ok else "critical")
            for name, res in health.items()
        ))
        if st.button("다시 불러오기", width="stretch"):
            st.cache_data.clear()
            st.rerun()

        st.caption(
            f"알람 기준: 예비율 {settings.reserve_warning_pct:g}% 미만 Warning, "
            f"{settings.reserve_critical_pct:g}% 미만 Critical (config/dashboard.yaml)"
        )
    return hour, period, adjust


def build_context() -> Context:
    """설정·서비스 상태·사이드바 입력을 모아 시계열을 계산한다."""
    settings = get_settings()
    urls = logic.service_urls()
    health = {name: fetch_health(url) for name, url in urls.items()}
    hour, (start, end), adjust = render_sidebar(settings, health)
    today = date.today()
    df = logic.sample_day(settings, today, adjust)
    period = (
        df if start == end == today else logic.sample_period(settings, start, end, adjust)
    )
    return Context(
        settings=settings, urls=urls, health=health, hour=hour, adjust=adjust,
        base=logic.sample_day(settings, today), df=df,
        now=df.loc[df.hour == hour].iloc[0], period=period, start=start, end=end,
        real=real_mix(urls["collector"]) if health["collector"].ok else None,
    )


def _ctx() -> Context:
    """main 에서 만든 Context."""
    return st.session_state["vpp_ctx"]


# ── 통합 관제 (한 화면) ──────────────────────────────────────────────────────


def render_adjust_notice(ctx: Context) -> None:
    """조정값이 기본값과 다르면 적용 중인 항목과 비교표(접힘)를 보여준다."""
    changed = _changed_adjust(ctx.settings)
    if not changed:
        return
    ss = st.session_state
    labels = {
        "adj_demand": f"수요 {ss.adj_demand}%", "adj_solar": f"태양광 {ss.adj_solar}%",
        "adj_wind": f"풍력 {ss.adj_wind}%", "adj_outage": f"원자력 탈락 {ss.adj_outage:,}MW",
        "adj_ess_power": f"ESS 출력 {ss.adj_ess_power:,}MW",
        "adj_ess_energy": f"ESS 용량 {ss.adj_ess_energy:,}MWh",
        "adj_ess_rte": f"ESS 효율 {ss.adj_ess_rte}%",
    }
    shed = ctx.df.shed_mw.sum()
    parts = [labels[k] for k in changed]
    if shed > 0:
        parts.append(f"⚠ 공급 부족 {logic.fmt_energy(shed, compact())}")
    # 한 줄로 접어 둔다 — 펼치면 기본값 대비 비교표
    with st.expander(f"**조정 적용 중** · {' · '.join(parts)} — 기본값 대비 비교"):
        render_compare_table(ctx)


def render_compare_table(ctx: Context) -> None:
    """조정 결과 vs 기본값 비교표."""
    table = logic.compare_table(ctx.settings, ctx.base, ctx.df, compact())

    def color_verdict(v: str) -> str:
        if v.startswith("✓"):
            return f"color:{C_GREEN};font-weight:600"
        if v.startswith("✗"):
            return f"color:{C_RED};font-weight:600"
        return ""

    st.dataframe(
        table.style.map(color_verdict, subset=["판정"]), hide_index=True, width="stretch"
    )


def card(stretch: bool = True, key: str | None = None) -> DeltaGenerator:
    """한 화면용 카드. stretch 면 같은 줄의 카드끼리 높이를 맞춘다.

    한 칸 안에 세로로 쌓는 카드는 stretch=False (내용 높이). key 를 주면 CSS 로 꾸밀 수 있다
    (.st-key-<key>).
    """
    if stretch:
        return st.container(border=True, height="stretch", key=key)
    return st.container(border=True, key=key)


def _bullet(
    value: float, vmax: float, color: str, ticks: list[float],
    zones: list[tuple[float, float, str]] | None = None,
    marker: float | None = None, vmin: float = 0.0, stripe_from: float | None = None,
) -> str:
    """가로 불릿 게이지 HTML.

    배경 구간(zones) 위에 현재값 막대를 채우고, 비교값(marker)을 세로선으로 표시한다.
    stripe_from 을 주면 그 값부터 막대 끝까지를 줄무늬로 칠한다 (공급능력의 예비력 구간).
    범위를 벗어난 값은 양 끝에 붙인다 (숫자는 카드 큰 값으로 따로 보인다).
    """

    def pos(v: float) -> float:
        return min(max((v - vmin) / (vmax - vmin), 0.0), 1.0) * 100

    bands = "".join(
        f'<i class="zone" style="left:{pos(a):.2f}%;width:{pos(b) - pos(a):.2f}%;'
        f'background:{c}"></i>'
        for a, b, c in zones or []
    )
    fill = (f'<i class="fill" style="width:{pos(value):.2f}%;'
            f'background:linear-gradient(90deg,{color}55,{color})"></i>')
    if stripe_from is not None and value > stripe_from:
        fill += (f'<i class="stripe" style="left:{pos(stripe_from):.2f}%;'
                 f'width:{pos(value) - pos(stripe_from):.2f}%"></i>')
    mark = f'<b class="mark" style="left:{pos(marker):.2f}%"></b>' if marker is not None else ""
    tick = "".join(f'<span style="left:{pos(t):.2f}%">{t:g}</span>' for t in ticks)
    return (f'<div class="vpp-bullet"><div class="track">{bands}{fill}{mark}</div>'
            f'<div class="ticks">{tick}</div></div>')


def _bullet_card(
    title: str, value: str, unit: str, delta_html: str, bullet: str, sub: str,
    value_color: str = C_BLUE, tag: str | None = None, stretch: bool = True,
) -> None:
    """수급 게이지 카드: 큰 값·변화 한 줄, 그 아래 카드 폭 전체를 쓰는 불릿 게이지.

    tag 를 주지 않으면 목업 태그를 단다.
    """
    with card(stretch):
        html(
            f'<div class="vpp-kpi-head"><span>{title}{mock_tag() if tag is None else tag}'
            "</span></div>"
            '<div class="vpp-kpi-row">'
            f'<span class="vpp-kpi-value" style="color:{value_color}">{value}'
            f'<span class="u">{unit}</span></span>{delta_html}</div>'
            f"{bullet}"
            + (f'<div class="vpp-bullet-sub">{sub}</div>' if sub else "")
        )


def render_demand_card(ctx: Context) -> None:
    """전력 수요 불릿 게이지 (기능 요구 10). collector 실측 최신값(5분)이 있으면 그것."""
    now, prev = ctx.now, ctx.prev
    scale = 1000 if compact() else 1
    gmax = 110000 / scale
    ticks = [0, 55000 / scale, gmax]
    res = fetch_collector(ctx.urls["collector"], "power_demand", 13)
    real = logic.latest_demand(res)
    if real:
        d_now, d_prev = real["mw"], real["prev_mw"]
        tag = real_tag(f'실측 {real["ts"]:%m/%d %H:%M}')
        delta = _delta(_pct_change(d_now, d_prev)) if d_prev else ""
    else:
        d_now, tag = now.demand_mw, None
        delta = _delta(_pct_change(now.demand_mw, prev.demand_mw))
    value, unit = pw(d_now).split(" ")
    _bullet_card(
        "전력 수요", value, unit, delta, _bullet(d_now / scale, gmax, C_BLUE, ticks),
        "1시간 전 대비" if real and d_prev else "", tag=tag, stretch=False,
    )


def render_supply_card(ctx: Context) -> None:
    """공급능력 불릿 게이지. 공급능력 = 가용 설비 + 재생 + ESS 출력
    (KPX 정의: 예비력 = 공급능력 − 수요). collector 에 없어 목업이다.
    """
    now, prev = ctx.now, ctx.prev
    scale = 1000 if compact() else 1
    gmax = 110000 / scale
    ticks = [0, 55000 / scale, gmax]
    cap, cap_prev = now.available_capacity_mw, prev.available_capacity_mw
    value, unit = pw(cap).split(" ")
    _bullet_card(
        "공급 능력", value, unit, _delta(_pct_change(cap, cap_prev)),
        _bullet(cap / scale, gmax, C_INDIGO, ticks, marker=now.demand_mw / scale,
                stripe_from=now.demand_mw / scale),
        f'<span class="vpp-key stripe"></span>예비력 <b>{pw(cap - now.demand_mw)}</b>',
        value_color=C_INDIGO, stretch=False,
    )


# 예비율 링의 최대값 (%). 넘는 값은 링을 가득 채운다
RESERVE_RING_MAX = 20.0


def _reserve_ring(value: float, color: str, label: str, css_class: str,
                  crit: float, warn: float) -> str:
    """예비율 링 게이지 HTML. 링 위의 눈금 두 개가 Critical·Warning 기준이다."""
    frac = min(max(value / RESERVE_RING_MAX, 0.0), 1.0)
    ticks = "".join(
        f'<i class="tick" style="transform:rotate({t / RESERVE_RING_MAX * 360:.1f}deg)"></i>'
        for t in (crit, warn)
    )
    return (
        f'<div class="vpp-ring" style="--p:{frac * 100:.1f};--c:{color}">{ticks}'
        f'<div class="in"><b style="color:{color}">{value:.1f}<small>%</small></b>'
        f"{badge(label, css_class)}</div></div>"
    )


def render_reserve_hero(ctx: Context) -> None:
    """벤토 히어로: 예비율 링 + 위기 시나리오 (둘 다 '예비율이 버티는가'를 본다)."""
    s, now, prev = ctx.settings, ctx.now, ctx.prev
    label, css_class = logic.reserve_status(s, now.reserve_pct)
    color = STATUS_COLOR[css_class]
    crit, warn = s.reserve_critical_pct, s.reserve_warning_pct
    with card(key="vpp-hero"):
        html(
            f'<div class="vpp-kpi-head"><span>예비율{mock_tag()}</span>'
            f"<small>{ctx.hour}시</small></div>"
            '<div class="vpp-alarm">'
            f"{_reserve_ring(now.reserve_pct, color, label, css_class, crit, warn)}</div>"
            '<div class="vpp-ring-sub">'
            f'{_delta(now.reserve_pct - prev.reserve_pct, "%p")}'
            '<span class="vpp-legend">'
            f'<span><i style="background:{C_RED}"></i>Critical &lt;{crit:g}%</span>'
            f'<span><i style="background:{C_YELLOW}"></i>Warning &lt;{warn:g}%</span></span>'
            "</div>"
        )
        _render_crisis(ctx)


def render_realtime_card(ctx: Context) -> None:
    """collector 실측 최신값 (수요·SMP). 값을 큰 타일로, 연결 상태는 갱신 주기 태그로."""
    demand = fetch_collector(ctx.urls["collector"], "power_demand", 1)
    smp = fetch_collector(ctx.urls["collector"], "smp", 1)
    status = (
        f'<span class="vpp-sync ok">{COLLECTOR_INTERVAL_MIN}분 갱신</span>' if demand.ok
        else '<span class="vpp-sync off">연결 끊김</span>'
    )
    with card():  # 오른쪽 칸 맨 아래: 남은 높이를 채운다
        html(f'<div class="vpp-kpi-head">KPX 실측{status}</div>')
        last_demand = logic.latest_row(demand) if demand.ok else None
        last_smp = logic.latest_row(smp) if smp.ok else None
        if not last_demand:
            html(f'<div class="vpp-kpi-sub vpp-empty">collector 응답 없음<br>'
                 f'{demand.error or "데이터 없음"}</div>')
            return
        value, unit = pw(last_demand["demand_mw"]).split(" ")
        tiles = [("수요", value, unit, C_BLUE)]
        if last_smp and last_smp.get("smp_won_per_kwh") is not None:
            tiles.append(("SMP", f"{last_smp['smp_won_per_kwh']:,.1f}", "원/kWh", C_ORANGE))
        html('<div class="vpp-stats-fill">' + "".join(
            f'<div class="vpp-tile"><span>{label}</span>'
            f'<b style="color:{color}">{v}<small>{u}</small></b></div>'
            for label, v, u, color in tiles
        ) + "</div>")


MERIT_PLAN, MERIT_REAL = "MILP 계획", "KPX 실측"


def render_merit_order(ctx: Context) -> None:
    """시간대별 발전원 스택 (연료비 순) + 수요선.

    MILP 계획과 collector 실측이 둘 다 있으면 카드 안에서 고른다 (기본 MILP 계획).
    하나만 있으면 그것, 둘 다 없으면 목업.
    """
    plan = milp_result(ctx)
    if plan is not None and not plan["units"]:
        plan = None
    sources = [name for name, ok in ((MERIT_PLAN, plan), (MERIT_REAL, ctx.real)) if ok]
    if sources:
        with card():
            head = st.empty()  # 제목은 고른 출처에 따라 달라서 토글 뒤에 채운다
            choice = sources[0]
            if len(sources) > 1:
                choice = st.segmented_control(
                    "Merit Order 출처", sources, default=sources[0], key="merit_source",
                    label_visibility="collapsed",
                ) or sources[0]
            height = H_MERIT - (40 if len(sources) > 1 else 0)
            if choice == MERIT_PLAN:
                _render_plan_mix(head, plan, height)
            else:
                _render_real_mix(head, ctx.real, height)
        return
    s, df = ctx.settings, ctx.df
    fleet = sorted(s.fleet, key=lambda f: f.fuel_cost_won_per_kwh)
    with card():
        html(f'<div class="vpp-kpi-head"><span>Merit Order 스택{mock_tag()}</span>'
             '<small>범례 = 연료비 낮은 순</small></div>')
        fig = px.bar(
            _melt_by_fuel(s, df), x="hour", y="mw", color="발전원",
            category_orders={"발전원": [f.name for f in fleet]},
            color_discrete_map={f.name: FUEL_COLOR[f.code] for f in s.fleet},
            labels={"hour": "시간", "mw": "MW", "발전원": ""},
        )
        fig.add_scatter(
            x=df["hour"], y=df["demand_mw"], mode="lines", name="수요",
            line=dict(color=C_BRIGHT, width=2, dash="dot"),
        )
        fig.add_vline(x=ctx.hour, line=dict(color="rgba(255,255,255,.35)", width=1))
        fig.update_traces(marker_line_width=0, selector=dict(type="bar"))
        fig.update_layout(barmode="stack", bargap=0.22)
        st.plotly_chart(_legend_right(_compact_chart(fig, H_MERIT)), width="stretch",
                        key="c_merit", config=NO_BAR)


@st.cache_data(show_spinner=False)
def unit_fuels() -> dict[str, str]:
    """유닛 id → 연료 코드 (config/generators.yaml, C 담당)."""
    return logic.load_unit_fuels()


def _render_plan_mix(head: DeltaGenerator, plan: dict, height: int) -> None:
    """MILP 계획의 발전원별 출력을 merit order(연료비 순)로 쌓고 순수요선.

    ESS 는 방전을 위(+), 충전을 아래(−)로 쌓는다. 태양광·풍력은 순수요에서 이미 빠져 있다.
    """
    stack = logic.milp_fuel_stack(plan, unit_fuels())
    codes = list(dict.fromkeys(stack["fuel"]))
    ko = {**COLLECTOR_FUEL_KO, **PLAN_EXTRA_KO}
    colors = {**COLLECTOR_FUEL_COLOR, **PLAN_EXTRA_COLOR}
    names = [ko.get(f, f) for f in codes]
    with head.container():
        html(f'<div class="vpp-kpi-head"><span>Merit Order 스택{milp_tag(plan)}</span>'
             '<small>범례 = 연료비 낮은 순 · 순수요 기준</small></div>')
    fig = px.bar(
        stack.assign(발전원=stack.fuel.map(lambda f: ko.get(f, f))),
        x="hour", y="mw", color="발전원",
        category_orders={"발전원": names},
        color_discrete_map={ko.get(f, f): colors.get(f, C_TEXT) for f in codes},
        labels={"hour": "시간", "mw": "MW", "발전원": ""},
    )
    fig.add_scatter(
        x=list(range(len(plan["demand_mw"]))), y=plan["demand_mw"], mode="lines",
        name="순수요", line=dict(color=C_BRIGHT, width=2, dash="dot"),
    )
    fig.update_traces(marker_line_width=0, selector=dict(type="bar"))
    fig.update_layout(barmode="relative", bargap=0.22)
    st.plotly_chart(_legend_right(_compact_chart(fig, height)), width="stretch",
                    key="c_merit", config=NO_BAR)


def _render_real_mix(head: DeltaGenerator, real: RealMix, height: int) -> None:
    """실측 발전 믹스 (collector trading_by_fuel) 를 연료비 순으로 쌓고 같은 날 실측 수요선."""
    names = [COLLECTOR_FUEL_KO.get(f, f) for f in real.order]
    mix = real.mix.assign(발전원=real.mix.fuel.map(lambda f: COLLECTOR_FUEL_KO.get(f, f)))
    demand = mix.groupby("hour")["demand_mw"].first()
    with head.container():
        html(f'<div class="vpp-kpi-head"><span>Merit Order 스택{real.tag}</span>'
             '<small>범례 = 연료비 낮은 순</small></div>')
    fig = px.bar(
        mix, x="hour", y="mw", color="발전원",
        category_orders={"발전원": names},
        color_discrete_map={COLLECTOR_FUEL_KO.get(f, f): COLLECTOR_FUEL_COLOR.get(f, C_TEXT)
                            for f in real.order},
        labels={"hour": "시간", "mw": "MW", "발전원": ""},
    )
    if demand.notna().any():
        fig.add_scatter(
            x=demand.index, y=demand.values, mode="lines", name="수요",
            line=dict(color=C_BRIGHT, width=2, dash="dot"),
        )
    fig.update_traces(marker_line_width=0, selector=dict(type="bar"))
    fig.update_layout(barmode="stack", bargap=0.22)
    st.plotly_chart(_legend_right(_compact_chart(fig, height)), width="stretch",
                    key="c_merit", config=NO_BAR)


def render_optimization(ctx: Context) -> None:
    """최적화 결과 — MILP 절감률·비용·응답·제약 + Two-Stage VSS. dispatch_api 가 없으면 목업."""
    s = ctx.settings
    live = milp_result(ctx)
    milp = live or logic.sample_dispatch_summary(ctx.df, ctx.settings)
    tag = milp_tag(live) if live else mock_tag("MILP 목업")
    latency_sub = f'풀이 {live["solve_s"]:.1f}초' if live else "MILP 풀이 시간"
    vss, vss_tag = stochastic_result(ctx)
    ok_saving = logic.meets(s, "milp_saving_pct", milp["saving_pct"])
    constraints = milp["constraints"]
    n_ok = sum(constraints.values())
    all_ok = n_ok == len(constraints)
    tip_rows = "".join(
        f'<span class="{"ok" if ok else "bad"}">{"✓" if ok else "✗"} {name}</span>'
        for name, ok in constraints.items()
    )
    def chip(key: str, value: float) -> str:
        """기준 충족 칩 (초록 ✓ / 빨강 ✗)."""
        ok = logic.meets(s, key, value)
        return (f'<em class="vpp-chk {"ok" if ok else "bad"}">'
                f'{logic.target_text(s, key)} {"✓" if ok else "✗"}</em>')

    def tile(label: str, value: str, sub: str, key: str | None = None, raw: float = 0) -> str:
        badge_html = chip(key, raw) if key else ""
        return (f'<div class="vpp-tile opt"><div><span>{label}</span><small>{sub}</small></div>'
                f"<div><b>{value}</b>{badge_html}</div></div>")

    with card():
        html(
            '<div class="vpp-kpi-head">'
            f'<span>최적화 결과{tag}</span><small>MILP · Two-Stage</small></div>'
            '<div class="vpp-kpi-row">'
            f'<span class="vpp-kpi-value" style="color:{C_GREEN if ok_saving else C_RED}">'
            f'{milp["saving_pct"]:.1f}<span class="u">%</span></span>'
            '<span class="vpp-hero-sub">비용 절감'
            f'{chip("milp_saving_pct", milp["saving_pct"])}</span></div>'
            '<div class="vpp-tiles">'
            + tile("MILP 비용" if live else "MILP 연료비", won(milp["milp_cost_won"]),
                   f'Rule-based {won(milp["rule_cost_won"])}')
            + tile("API 응답", f'{milp["latency_s"]:.1f}초', latency_sub,
                   "milp_latency_s", milp["latency_s"])
            + tile(f"VSS{vss_tag}", f'{vss["vss_pct"]:.1f}%',
                   f'Two-Stage · 시나리오 {vss["n_scenarios"]}개'
                   + (f' · 예비율 {vss["reserve_ratio"]:.0%}' if "reserve_ratio" in vss else ""),
                   "vss_pct", vss["vss_pct"])
            + "</div>"
        )
        html(
            f'<div class="vpp-checktip {"ok" if all_ok else "bad"}" tabindex="0">'
            f'<span class="ic">{"✓" if all_ok else "✗"}</span>'
            f"<span>제약 조건 {n_ok}/{len(constraints)}</span>"
            f'<div class="tip"><b>MILP 제약 조건</b>{tip_rows}</div></div>'
        )


def _scenario_reserve(row: pd.Series, scenario: logic.CrisisScenario) -> float:
    """시나리오 반영 예비율 (%)."""
    demand, capacity = logic.apply_scenario(row, scenario)
    return (capacity - demand) / demand * 100


def _render_crisis(ctx: Context) -> None:
    """위기 시나리오 시뮬레이션 — 선택 시나리오 적용 결과 + 3종 하루 최저 예비율.

    예비율 히어로 카드 안에서 그린다 (render_reserve_hero).
    """
    s = ctx.settings
    names = [sc.name for sc in s.crisis]
    # 바로 아래가 위젯이라 Streamlit 마크다운의 음수 하단 여백(-1rem)을 메운다
    html('<div class="vpp-kpi-head vpp-divider" style="margin-bottom:1.65rem">'
         f'<span>위기 시나리오{mock_tag()}</span>'
         f"<small>예비율 ≥ {s.targets['crisis_reserve_pct']:g}% 유지</small></div>")
    choice = st.selectbox("시나리오", ["없음", *names], key="crisis_scenario",
                          label_visibility="collapsed")
    base = ctx.now.reserve_pct
    new = base if choice == "없음" else _scenario_reserve(
        ctx.now, s.crisis[names.index(choice)]
    )
    label, css_class = logic.reserve_status(s, new)
    html(
        '<div class="vpp-kpi-row">'
        f'<span class="vpp-kpi-sub">{ctx.hour}시 평시 {base:.1f}% →</span>'
        f'<span class="vpp-kpi-value" style="color:{STATUS_COLOR[css_class]}">'
        f'{new:.1f}<span class="u">%</span></span>{badge(label, css_class)}</div>'
    )

    lo, hi, crit = -10.0, 30.0, s.targets["crisis_reserve_pct"]

    def pos(v: float) -> float:
        return min(max((v - lo) / (hi - lo), 0.0), 1.0) * 100

    rows = []
    for _, r in logic.crisis_table(ctx.df, s).iterrows():
        v, ok = r["최저 예비율(%)"], bool(r[logic.crisis_ok_column(s)])
        color = C_GREEN if ok else C_RED
        rows.append(
            f'<div class="vpp-crisis-row"><span class="n">{r["시나리오"]}</span>'
            f'<span class="bar"><i style="width:{pos(v):.1f}%;background:{color}"></i>'
            f'<em style="left:{pos(crit):.1f}%"></em></span>'
            f'<b style="color:{color}">{v:.1f}%</b></div>'
        )
    html(f'<div class="vpp-crisis-cap">하루 중 최저 예비율 (세로선 = {crit:g}%)</div>'
         + "".join(rows))


def _period_x(ctx: Context) -> pd.Series:
    """기간 차트 x축: 하루면 시(0~23), 여러 날이면 시각."""
    return ctx.period.hour if ctx.days == 1 else ctx.period.ts


def _period_axis(fig: go.Figure, ctx: Context) -> go.Figure:
    """여러 날이면 x축을 날짜 눈금으로."""
    if ctx.days > 1:
        fig.update_xaxes(tickformat="%m/%d", dtick=86400000 * max(1, ctx.days // 7))
    return fig


def _render_real_carbon(real: RealMix) -> None:
    """실측 하루의 시간대별 탄소 배출량 = 발전원별 실측 출력 x 배출계수."""
    carbon = logic.real_carbon_by_hour(real.mix, real.co2)
    with card():
        html(
            f'<div class="vpp-kpi-head"><span>탄소 배출량 추이{real.tag}</span>'
            '<small>실측 출력 x 배출계수</small></div>'
            '<div class="vpp-kpi-row">'
            f'<span class="vpp-kpi-value sm">{ton(carbon.sum())}</span>'
            '<span class="vpp-kpi-sub">일간 · 실제 계통 (ESS 비교 없음)</span></div>'
        )
        fig = go.Figure()
        fig.add_scatter(
            x=carbon.index, y=carbon.values, mode="lines", name="실측", fill="tozeroy",
            line=dict(color=C_ORANGE, width=2.5, shape="spline"),
            fillgradient=dict(type="vertical", colorscale=[[0, "rgba(251,146,60,0)"],
                                                           [1, "rgba(251,146,60,.35)"]]),
        )
        st.plotly_chart(_compact_chart(fig, H_SMALL), width="stretch", key="c_carbon",
                        config=NO_BAR)


def _signed_pct(value: float) -> str:
    """증감률을 색과 부호를 붙여 표시한다 (배출이 늘면 빨강)."""
    return (f'<b style="color:{C_RED if value > 0 else C_GREEN}">'
            f'{"+" if value > 0 else ""}{value:.2f}%</b>')


def _render_milp_carbon(with_ess: dict, no_ess: dict) -> None:
    """MILP 계획 하루의 시간대별 배출 (ESS 연계 / ESS 없음) 과 Rule-based 대비 증감."""
    t = logic.carbon_tradeoff(with_ess, no_ess)
    hours = list(range(len(with_ess["carbon_hourly_ton"])))
    with card():
        html(
            f'<div class="vpp-kpi-head"><span>탄소 배출량 추이{milp_tag(with_ess)}</span>'
            '<small>MILP 급전 x 배출계수</small></div>'
            '<div class="vpp-kpi-row">'
            f'<span class="vpp-kpi-value sm">{ton(t["with_ess_ton"])}</span>'
            f'<span class="vpp-kpi-sub">일간 · ESS 연계 {_signed_pct(t["ess_pct"])}'
            f' · Rule-based 대비 {_signed_pct(t["rule_pct"])}</span></div>'
        )
        fig = go.Figure()
        fig.add_scatter(
            x=hours, y=with_ess["carbon_hourly_ton"], mode="lines", name="ESS 연계",
            fill="tozeroy", line=dict(color=C_ORANGE, width=2.5, shape="spline"),
            fillgradient=dict(type="vertical", colorscale=[[0, "rgba(251,146,60,0)"],
                                                           [1, "rgba(251,146,60,.35)"]]),
        )
        fig.add_scatter(
            x=hours, y=no_ess["carbon_hourly_ton"], mode="lines", name="ESS 없음",
            line=dict(color=C_TEXT, width=1.4, dash="dot", shape="spline"),
        )
        st.plotly_chart(_compact_chart(fig, H_SMALL), width="stretch", key="c_carbon",
                        config=NO_BAR)


def render_carbon(ctx: Context) -> None:
    """시간대별 탄소 배출량.

    우선순위: MILP 계획(ESS 연계 / ESS 없음 비교) → collector 실측 하루 → 분석 기간 목업.
    """
    pair = milp_carbon_pair(ctx)
    if pair is not None:
        _render_milp_carbon(*pair)
        return
    if ctx.real is not None:
        _render_real_carbon(ctx.real)
        return
    df, x = ctx.period, _period_x(ctx)
    total, base = df.carbon_ton.sum(), df.carbon_no_ess_ton.sum()
    diff = total - base
    with card():
        html(
            f'<div class="vpp-kpi-head"><span>탄소 배출량 추이{mock_tag()}</span>'
            f'<small>{ctx.period_label}</small></div>'
            '<div class="vpp-kpi-row">'
            f'<span class="vpp-kpi-value sm">{ton(total)}</span>'
            f'<span class="vpp-kpi-sub">{"일간" if ctx.days == 1 else "기간 합계"} · ESS 연계 '
            f'<b style="color:{C_RED if diff > 0 else C_GREEN}">'
            f'{"+" if diff > 0 else ""}{diff / base * 100:.2f}%</b></span></div>'
        )
        fig = go.Figure()
        fig.add_scatter(
            x=x, y=df.carbon_ton, mode="lines", name="ESS 연계", fill="tozeroy",
            line=dict(color=C_ORANGE, width=2.5, shape="spline"),
            fillgradient=dict(type="vertical", colorscale=[[0, "rgba(251,146,60,0)"],
                                                           [1, "rgba(251,146,60,.35)"]]),
        )
        fig.add_scatter(
            x=x, y=df.carbon_no_ess_ton, mode="lines", name="ESS 없음",
            line=dict(color=C_TEXT, width=1.4, dash="dot", shape="spline"),
        )
        st.plotly_chart(_period_axis(_compact_chart(fig, H_SMALL), ctx), width="stretch",
                        key="c_carbon", config=NO_BAR)


def render_ess(ctx: Context) -> None:
    """ESS 충/방전·SoC 와 피크 감소율.

    dispatch_api 가 있으면 MILP(peak_shaving) 계획 하루치, 없으면 분석 기간 목업
    (기간 최대 피크 기준).
    """
    s = ctx.settings
    live = milp_result(ctx, "peak_shaving")
    planned = bool(live and live["ess"])
    if planned:
        ess = live["ess"]
        df = logic.milp_ess_frame(ess)
        x = df["hour"]
        before, after = ess["peak_before_mw"], ess["peak_after_mw"]
        cut_pct = ess["peak_reduction_pct"]
        tag, period = milp_tag(live, "MILP·peak"), f'{live["day"]:%m/%d} 하루'
    else:
        df, x = ctx.period, _period_x(ctx)
        before, after, cut_pct = logic.peak_reduction(df)
        tag, period = mock_tag(), ctx.period_label
    ok = logic.meets(s, "ess_peak_cut_pct", cut_pct)
    with card():
        html(
            f'<div class="vpp-kpi-head"><span>ESS 충/방전 · SoC{tag}</span>'
            f'<small>{period} · {logic.target_text(s, "ess_peak_cut_pct")}</small></div>'
            '<div class="vpp-kpi-row">'
            f'<span class="vpp-kpi-value sm" style="color:{C_GREEN if ok else C_RED}">'
            f'{cut_pct:.1f}%</span>'
            f'<span class="vpp-kpi-sub">피크 감소 {pw(before)} → {pw(after)}</span></div>'
        )
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_bar(x=x, y=df["ess_discharge_mw"], name="방전", marker_color=C_BLUE)
        fig.add_bar(x=x, y=-df["ess_charge_mw"], name="충전", marker_color=C_ORANGE)
        fig.add_scatter(
            x=x, y=df["ess_soc_pct"], name="SoC",
            line=dict(color=C_INDIGO, width=2, shape="spline"), secondary_y=True,
        )
        fig.update_traces(marker_line_width=0, marker_cornerradius=3, selector=dict(type="bar"))
        fig.update_layout(barmode="relative", bargap=0.2 if planned or ctx.days == 1 else 0)
        fig = _compact_chart(fig, H_SMALL)
        if not planned:
            fig = _period_axis(fig, ctx)
        fig.update_yaxes(range=[0, 100], secondary_y=True, gridcolor="rgba(0,0,0,0)",
                         showticklabels=False)
        st.plotly_chart(fig, width="stretch", key="c_ess", config=NO_BAR)


GEN_CAPTION = '<div class="vpp-kpi-sub vpp-gen-cap">막대 = 이용률 (출력 ÷ 설비용량)</div>'


def _render_real_generators(real: RealMix, hour: int) -> None:
    """실측 하루의 그 시각 발전원별 출력과 이용률 (출력 / 설비용량)."""
    rows = []
    for r in logic.real_utilization(real.mix, hour).itertuples():
        color = COLLECTOR_FUEL_COLOR.get(r.fuel, C_TEXT)
        # 아이콘이 없는 발전원도 그리드 첫 칸을 채워야 열이 밀리지 않는다
        icon = (gen_icon(REAL_ICON[r.fuel], 18) if r.fuel in REAL_ICON
                else f'<span class="vpp-dot" style="background:{color}"></span>')
        rows.append(
            f'<div class="vpp-gen-row">{icon}'
            f'<span class="n">{COLLECTOR_FUEL_KO.get(r.fuel, r.fuel)}</span><b>{pw(r.mw)}</b>'
            f'<span class="bar"><i style="width:{r.util_pct:.1f}%;background:{color}"></i></span>'
            f'<small>{r.util_pct:.0f}%</small></div>'
        )
    with card():
        html(f'<div class="vpp-kpi-head"><span>발전원별 출력{real.tag}</span>'
             f"<small>{hour}시</small></div>{GEN_CAPTION}" + "".join(rows))


def render_generators(ctx: Context) -> None:
    """발전원별 현재 출력과 이용률 (목록형). 실측이 있으면 실측 하루의 같은 시각."""
    if ctx.real is not None:
        _render_real_generators(ctx.real, ctx.hour)
        return
    rows = []
    for fuel in ctx.settings.fleet:
        mw = ctx.now[f"{fuel.code}_mw"]
        color = FUEL_COLOR[fuel.code]
        usage = min(mw / fuel.capacity_mw * 100, 100.0)
        rows.append(
            f'<div class="vpp-gen-row">{gen_icon(fuel.code, 18)}'
            f'<span class="n">{fuel.name}</span><b>{pw(mw)}</b>'
            f'<span class="bar"><i style="width:{usage:.1f}%;background:{color}"></i></span>'
            f'<small>{usage:.0f}%</small></div>'
        )
    with card():
        html(f'<div class="vpp-kpi-head"><span>발전원별 출력{mock_tag()}</span>'
             f"<small>{ctx.hour}시</small></div>{GEN_CAPTION}"
             + "".join(rows))


def page_overview() -> None:
    """통합 관제 — 필수 항목을 스크롤 없이 한 화면에 둔다."""
    ctx = _ctx()
    render_adjust_notice(ctx)

    # 벤토 그리드. 1행: 예비율 히어로(세로로 김) · Merit Order(넓게) · 수요/공급/실측(세로로 쌓음)
    hero, merit, side = st.columns([1.05, 2.2, 1.05])
    with hero:
        render_reserve_hero(ctx)
    with merit:
        render_merit_order(ctx)
    with side:
        render_demand_card(ctx)
        render_supply_card(ctx)
        render_realtime_card(ctx)

    # 2행: 최적화 · 탄소(넓게) · ESS(넓게) · 발전원
    c1, c2, c3, c4 = st.columns([1.25, 1.25, 1.25, 1.0])
    with c1:
        render_optimization(ctx)
    with c2:
        render_carbon(ctx)
    with c3:
        render_ess(ctx)
    with c4:
        render_generators(ctx)


# ── 분석 상세 ────────────────────────────────────────────────────────────────


def render_forecast(ctx: Context) -> None:
    """168h 수요 예측 + 90% PI 와 예측 성능 지표. forecast_api 가 없으면 목업."""
    settings = ctx.settings
    fc, metrics, live, latency = forecast_result(ctx)
    panel_title("수요 예측 · 90% 예측구간 (168h)", source_tag(live, latency))

    fig = go.Figure()
    fig.add_scatter(
        x=fc.hour, y=fc.demand_hi_mw, mode="lines", line=dict(width=0),
        showlegend=False, hoverinfo="skip",
    )
    fig.add_scatter(
        x=fc.hour, y=fc.demand_lo_mw, mode="lines", line=dict(width=0), fill="tonexty",
        fillcolor="rgba(56,189,248,.12)", name="90% PI",
    )
    fig.add_scatter(x=fc.hour, y=fc.demand_fc_mw, mode="lines", name="예측",
                    line=dict(color=C_BLUE, width=2.5, shape="spline"))
    if "demand_actual_mw" in fc:  # 목업만 실측이 있다 (forecast_api 는 미래 예측)
        fig.add_scatter(x=fc.hour, y=fc.demand_actual_mw, mode="lines", name="실측",
                        line=dict(color=C_ORANGE, width=1.6, shape="spline"))
    fig.add_vline(x=23.5, line=dict(color="rgba(255,255,255,.35)", dash="dash"))
    fig.update_layout(legend=dict(orientation="h", y=-0.2),
                      xaxis_title="예측 시점 (h ahead)", yaxis_title="MW")
    st.plotly_chart(style_dark(fig, height=300), width="stretch", key="c_forecast")

    if not live:
        html(
            kpi_badge(settings, "mape_24h_pct", "MAPE 24h", metrics["mape_24h_pct"])
            + kpi_badge(settings, "mape_168h_pct", "MAPE 168h", metrics["mape_168h_pct"])
            + kpi_badge(settings, "pi_coverage_pct", "PI Coverage", metrics["pi_coverage_pct"])
            + kpi_badge(settings, "nmae_solar_pct", "태양광 nMAE", metrics["nmae_solar_pct"])
            + kpi_badge(settings, "nmae_wind_pct", "풍력 nMAE", metrics["nmae_wind_pct"])
        )
        st.caption("목업 — forecast_api 응답 없음. nMAE 는 설비용량 기준 정규화. "
                   "점선 왼쪽이 24h ahead 구간.")
        return

    # forecast_api 지표는 정의가 스펙 지표와 달라 기준 배지는 참고용이다 (logic.parse_forecast)
    badges = kpi_badge(settings, "mape_24h_pct", "MAPE 1h", metrics["mape_1h_pct"])
    badges += kpi_badge(settings, "pi_coverage_pct", "PI Coverage", metrics["pi_coverage_pct"])
    for code, label in (("solar", "태양광"), ("wind", "풍력")):
        if f"nmae_{code}_pct" in metrics:
            badges += kpi_badge(settings, f"nmae_{code}_pct", f"{label} nMAE",
                                metrics[f"nmae_{code}_pct"])
    html(badges)
    re_start = metrics.get("solar_start")
    st.caption(
        f"{metrics['model']} · 예측 시작 {fc.ts.iloc[0]:%m/%d %H}시 (수요 데이터 끝 다음 시각). "
        "지표는 forecast_api 가 보정 구간에서 잰 값이다 — MAPE 는 1시간 앞 예측이라 "
        "24h·168h 기준과 다르고, nMAE 는 실측 평균 기준, Coverage 는 구간을 맞춘 같은 "
        "구간에서 잰 값이다. 168h MAPE 는 백테스트가 없어 표시하지 않는다."
        + (f" 태양광·풍력 예측은 {re_start:%m/%d}부터 (발전원 데이터 끝)." if re_start else "")
    )


def render_optimization_detail(ctx: Context) -> None:
    """MILP 비용·제약과 Two-Stage RP/EV/EEV."""
    s = ctx.settings
    live = milp_result(ctx)
    milp = live or logic.sample_dispatch_summary(ctx.df, ctx.settings)
    tag = milp_tag(live) if live else mock_tag("MILP 목업")
    vss, vss_tag = stochastic_result(ctx, "VSS")
    panel_title("최적화 상세 (MILP · Two-Stage)", tag + vss_tag)
    c1, c2, c3 = st.columns(3)
    c1.metric("MILP 비용 (일간)" if live else "MILP 연료비 (일간)", won(milp["milp_cost_won"]))
    c2.metric("Rule-based 비용" if live else "Rule-based 연료비", won(milp["rule_cost_won"]))
    c3.metric("불확실성 시나리오", f"{vss['n_scenarios']}개")  # Two-Stage 시나리오 수
    c4, c5, c6 = st.columns(3)
    c4.metric("EV", won(vss["ev_won"]))
    c5.metric("EEV", won(vss["eev_won"]))
    c6.metric("RP", won(vss["rp_won"]))
    html(
        kpi_badge(s, "milp_saving_pct", "절감률", milp["saving_pct"])
        + kpi_badge(s, "milp_latency_s", "응답", milp["latency_s"])
        + kpi_badge(s, "vss_pct", "VSS", vss["vss_pct"])
    )
    cond = (
        f' 계산 조건: 시나리오 {vss["n_scenarios"]}개 (seed {vss["seed"]}), '
        f'예비율 {vss["reserve_ratio"]:.0%}, 풀이 {vss["status"]} {vss["solve_time_s"]:.0f}초 '
        f'(문제당 제한 {vss["time_limit_s"]:.0f}초). 예비율 10% 에서는 시나리오 폭보다 '
        "예비율이 커서 VSS 가 0 에 가깝다 (formulation.md 3장 측정표)."
        if "seed" in vss else ""
    )
    st.caption(
        "VSS = EEV − RP, VSS% = VSS / EEV. EEV 는 평균 시나리오(EV) 해의 1단계를 고정하고 "
        "전 시나리오로 평가한 기대비용 (docs/formulation.md 3장)." + cond
    )
    if live:
        # dispatch_api 가 남긴 모델링 선택·가정 (절감률 비교 조건 등). 그대로 보여 준다
        st.caption("MILP 조건: " + " · ".join(live["notes"]))


def render_crisis_detail(ctx: Context) -> None:
    """시나리오 3종을 관측 시각에 적용한 게이지 + 하루 최저 예비율 표."""
    s = ctx.settings
    panel_title(f"위기 시나리오 상세 ({ctx.hour}시 적용)", mock_tag())
    # 게이지 눈금 범위는 모두 같게 둔다 — 다르면 같은 값도 바늘 위치가 달라 보인다.
    steps = _reserve_steps(s, value_min=-30)
    cols = st.columns(len(s.crisis) + 1)
    items = [("평시", ctx.now.reserve_pct)] + [
        (sc.name, _scenario_reserve(ctx.now, sc)) for sc in s.crisis
    ]
    for i, (col, (name, value)) in enumerate(zip(cols, items, strict=True)):
        with col:
            gauge_label(name)
            st.plotly_chart(
                _gauge_figure(value, 30, suffix="%", steps=steps, value_min=-30,
                              bar_color=STATUS_COLOR[logic.reserve_status(s, value)[1]]),
                width="stretch", key=f"g_crisis_{i}",
            )
    st.dataframe(logic.crisis_table(ctx.df, s), hide_index=True, width="stretch")


def render_carbon_detail(ctx: Context) -> None:
    """비용-탄소 Trade-off (MILP 계획) + 발전원별 배출 내역 (실측 하루, 없으면 분석 기간 목업)."""
    pair = milp_carbon_pair(ctx)
    if pair is not None:
        with_ess, no_ess = pair
        t = logic.carbon_tradeoff(with_ess, no_ess)
        panel_title(f"비용-탄소 Trade-off ({with_ess['day']:%m/%d} 계획)", milp_tag(with_ess))
        st.dataframe(
            logic.carbon_plan_table(with_ess, no_ess), hide_index=True, width="stretch",
            column_config={
                "Rule-based 대비 배출": st.column_config.NumberColumn(format="%+.2f%%"),
                "Rule-based 대비 비용": st.column_config.NumberColumn(format="%+.2f%%"),
            },
        )
        st.dataframe(logic.milp_carbon_by_fuel(with_ess, no_ess, COLLECTOR_FUEL_KO),
                     hide_index=True, width="stretch")
        st.caption(
            "같은 날·같은 수요로 푼 세 계획의 배출과 비용 (/dispatch/milp, cost 모드). "
            f"최적화(ESS 없음)는 Rule-based 대비 배출 {t['rule_pct']:+.2f}%, ESS 연계는 "
            f"{t['ess_pct']:+.2f}%. 연료비가 싼 석탄을 더 쓰면 비용은 줄고 배출은 늘 수 있고, "
            "ESS 는 왕복효율 손실만큼 추가 발전이 필요하다 (formulation.md 5.7). Rule-based 는 "
            "ESS 를 쓰지 않아 ESS 없는 MILP 와 같은 조건이다."
        )
    if ctx.real is not None:
        real = ctx.real
        panel_title("탄소 분석 (발전원별 배출)", real.tag)
        table = logic.real_carbon_by_fuel(real.mix, real.co2, COLLECTOR_FUEL_KO, real.order)
        st.dataframe(table, hide_index=True, width="stretch")
        st.caption(
            f"{real.day:%m/%d} 하루 실측 발전량 x 배출계수. 합계 "
            f"{ton(table['배출량(tCO2)'].sum())}. 배출계수는 config/generators.yaml (IAEA 2022 "
            "전과정 기준, 유류·국내탄은 출처 없는 가정값). 태양광·풍력·기타 재생·양수는 0 으로 "
            "본다. 사이드바 분석 기간과 ESS 비교는 실측에 적용하지 않는다."
        )
        return
    panel_title(f"탄소 · ESS 분석 ({ctx.period_label})", mock_tag())
    st.dataframe(logic.carbon_by_fuel(ctx.period, ctx.settings), hide_index=True,
                 width="stretch")
    if ctx.days > 1:
        st.dataframe(
            logic.daily_summary(ctx.period), hide_index=True, width="stretch",
            column_config={"날짜": st.column_config.DateColumn(format="MM/DD (ddd)")},
        )
    ess = ctx.adjust.ess_spec(ctx.settings.ess)
    st.caption(
        f"야간 충전분을 석탄({ctx.settings.fuel['coal'].co2_t_per_mwh:.2f})이 대고 "
        f"저녁 방전이 LNG({ctx.settings.fuel['lng'].co2_t_per_mwh:.2f})를 대체하면 "
        "피크는 줄어도 배출은 늘 수 있다 — 비용·탄소 Trade-off 의 한 예. "
        f"ESS {pw(ess.power_mw)} / {logic.fmt_energy(ess.energy_mwh, compact())}, "
        f"왕복효율 {ess.round_trip_efficiency:.0%} (가정값)."
    )


def page_analysis() -> None:
    """분석 상세 — 예측 성능, 최적화 수치, 위기 시나리오 게이지, 배출 내역."""
    ctx = _ctx()
    html('<div class="vpp-topbar"><div class="vpp-title">분석 상세</div>'
         '<div class="vpp-subtitle">예측 · 최적화 · 위기 시나리오 · 탄소</div></div>')
    with st.container(border=True):
        render_forecast(ctx)
    c1, c2 = st.columns([1, 1])
    with c1, st.container(border=True):
        render_optimization_detail(ctx)
    with c2, st.container(border=True):
        render_carbon_detail(ctx)
    with st.container(border=True):
        render_crisis_detail(ctx)


# ── 시간대 데이터 ────────────────────────────────────────────────────────────


def render_realtime_mix(collector_url: str) -> None:
    """collector 최신 발전원별 출력."""
    mix = fetch_collector(collector_url, "trading_by_fuel", 20)
    panel_title("KPX 발전원별 시간대 거래량 (collector)",
                source_tag(True, mix.latency_s) if mix.ok else "")
    fuel_mw = logic.latest_mix(mix) if mix.ok else {}
    if not fuel_mw:
        st.caption(f"collector 응답 없음 ({mix.error or '데이터 없음'})")
        return
    mix_df = pd.DataFrame(
        {"발전원": [COLLECTOR_FUEL_KO.get(k, k) for k in fuel_mw], "MW": list(fuel_mw.values())}
    ).sort_values("MW")
    fig = px.bar(mix_df, x="MW", y="발전원", orientation="h")
    fig.update_traces(marker_color=C_BLUE, marker_line_width=0, marker_cornerradius=6)
    fig.update_yaxes(title=None)
    st.plotly_chart(style_dark(fig, height=260), width="stretch", key="c_mix")


def render_generation_heatmap(
    settings: Settings, df: pd.DataFrame, real: RealMix | None = None,
) -> None:
    """발전원 x 시간 출력 히트맵. real 이 있으면 실측 하루."""
    if real is not None:
        panel_title("발전원별 시간대 출력 히트맵", real.tag)
        names = [COLLECTOR_FUEL_KO.get(f, f) for f in real.order]
        mix = real.mix.assign(발전원=real.mix.fuel.map(lambda f: COLLECTOR_FUEL_KO.get(f, f)))
        pivot = mix.pivot(index="발전원", columns="hour", values="mw").reindex(names[::-1])
        fig = px.imshow(
            pivot, aspect="auto", color_continuous_scale=HEAT_SCALE,
            labels={"x": "시간", "y": "발전원", "color": "MW"},
        )
        fig.update_coloraxes(colorbar=dict(outlinewidth=0, tickfont=dict(color=C_TEXT, size=11)))
        st.plotly_chart(style_dark(fig, height=260), width="stretch", key="c_heatmap")
        return
    panel_title("발전원별 시간대 출력 히트맵", mock_tag())
    order = [f.name for f in settings.fleet][::-1]
    pivot = _melt_by_fuel(settings, df).pivot(index="발전원", columns="hour", values="mw")
    fig = px.imshow(
        pivot.reindex(order), aspect="auto", color_continuous_scale=HEAT_SCALE,
        labels={"x": "시간", "y": "발전원", "color": "MW"},
    )
    fig.update_coloraxes(colorbar=dict(outlinewidth=0, tickfont=dict(color=C_TEXT, size=11)))
    st.plotly_chart(style_dark(fig, height=260), width="stretch", key="c_heatmap")


def render_hourly_table(settings: Settings, df: pd.DataFrame) -> None:
    """시간대 상세표와 CSV 내려받기."""
    panel_title("시간대 상세표", mock_tag())
    table = logic.hourly_table(settings, df)
    crit, warn = settings.reserve_critical_pct, settings.reserve_warning_pct

    def color_reserve(v: float) -> str:
        if v < crit:
            return "background-color:rgba(248,113,113,.25)"
        if v < warn:
            return "background-color:rgba(250,204,21,.22)"
        return ""

    styled = (
        table.style.map(color_reserve, subset=["예비율(%)"])
        .format("{:,.0f}", subset=[c for c in table.columns if c.endswith("(MW)")
                                   or c in ("배출(tCO2)", "ESS(MW, +방전)")])
        .format("{:.1f}", subset=["SoC(%)", "예비율(%)"])
        .format("{:,.2f}", subset=["연료비(억원)"])
    )
    st.dataframe(styled, hide_index=True, width="stretch", height=420)
    st.download_button(
        "CSV 내려받기", table.to_csv(index=False).encode("utf-8-sig"),
        file_name="dispatch_hourly.csv", mime="text/csv",
    )


def page_data() -> None:
    """시간대 데이터 — 실측, 히트맵, 상세표."""
    ctx = _ctx()
    html('<div class="vpp-topbar"><div class="vpp-title">시간대 데이터</div>'
         '<div class="vpp-subtitle">KPX 실측 · 발전원별 출력 · 24시간 상세표</div></div>')
    c1, c2 = st.columns([1, 1.6])
    with c1, st.container(border=True):
        render_realtime_mix(ctx.urls["collector"])
    with c2, st.container(border=True):
        render_generation_heatmap(ctx.settings, ctx.df, ctx.real)
    with st.container(border=True):
        render_hourly_table(ctx.settings, ctx.df)


LOADER_HTML = Path(__file__).with_name("loader.html")


def show_loader() -> DeltaGenerator | None:
    """세션 첫 실행에만 전체 화면 로딩 그림을 띄운다. 지울 자리(placeholder)를 돌려준다.

    첫 실행은 서비스 상태 확인·실측 데이터 호출로 몇 초 걸리는데, 그동안 빈 화면이 보이지
    않게 한다. 그림은 JS 로 그리므로 iframe(components.html)에 넣고, 그 iframe 을 감싼
    컨테이너(st-key-vpp-loader)를 화면 전체에 고정한다. 페이지가 다 그려지면 main 이 지운다.
    """
    if st.session_state.get("vpp_loaded") or not LOADER_HTML.exists():
        return None
    slot = st.empty()
    with slot.container(key="vpp-loader"):
        st.markdown(
            "<style>.st-key-vpp-loader{position:fixed;inset:0;z-index:1000000;"
            f"background:{C_BG}}}.st-key-vpp-loader iframe{{position:fixed;inset:0;"
            "width:100vw;height:100vh;border:0}</style>",
            unsafe_allow_html=True,
        )
        components.html(LOADER_HTML.read_text(encoding="utf-8"), height=600)
    return slot


def main() -> None:
    """페이지 설정·테마를 한 번 적용하고 페이지를 띄운다."""
    st.set_page_config(page_title="VPP 통합 관제 대시보드", page_icon="⚡", layout="wide")
    loader = show_loader()
    try:
        _run_pages()
    finally:
        # 페이지가 예외로 끝나도 로딩 그림이 오류를 가리지 않게 지운다
        if loader is not None:
            loader.empty()
            st.session_state["vpp_loaded"] = True


def _run_pages() -> None:
    """테마 CSS·내비게이션·공용 컨텍스트를 만들고 고른 페이지를 그린다."""
    inject_custom_css()
    nav = st.navigation([
        st.Page(page_overview, title="통합 관제", icon=":material/monitoring:", default=True),
        st.Page(page_analysis, title="분석 상세", icon=":material/analytics:",
                url_path="analysis"),
        st.Page(page_data, title="시간대 데이터", icon=":material/table_chart:",
                url_path="data"),
        st.Page(
            page_scenarios.render, title="시나리오 생성기",
            icon=":material/stacked_line_chart:", url_path="scenarios",
        ),
        st.Page(
            page_stochastic_test.render, title="Two-Stage 테스트",
            icon=":material/science:", url_path="stochastic-test",
        ),
    ])
    # 사이드바 조정은 페이지 밖(여기)에서 그려야 페이지를 옮겨도 값이 유지된다.
    if nav.url_path not in ("scenarios", "stochastic-test"):
        st.session_state["vpp_ctx"] = build_context()
    nav.run()


if __name__ == "__main__":
    main()
