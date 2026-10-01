"""dashboard — 통합 관제 대시보드 (Streamlit :8501).

화면만 그린다. 설정 로드·REST 호출·계산은 logic.py 에 있고, 판정 기준과 목업 설비값은
config/dashboard.yaml 에 있다.

페이지 구성:
    통합 관제      스크롤 없이 한 화면 — 수급 게이지·알람, Merit Order, 최적화 결과,
                   탄소 추이, ESS, 위기 시나리오 (CLAUDE.md 최종 결과물 4·기능 요구 10)
    분석 상세      168h 예측·성능 지표, 최적화 상세, 위기 시나리오 게이지, 배출 내역
    시간대 데이터  KPX 실측, 출력 히트맵, 시간대 상세표·CSV
    시나리오 생성기 page_scenarios.py
사이드바의 관측 시각·조정값은 앞의 세 페이지가 함께 쓴다 (main 에서 한 번 그린다).

데이터 출처 (모두 REST, 다른 서비스 코드는 import 하지 않는다):
    collector     GET  /data/{name}                    실측 수요·발전원별 출력·SMP
    forecast_api  GET  /health                         연결 상태
    dispatch_api  GET  /health                         연결 상태
                  POST /api/v1/dispatch/stochastic     VSS (shared/schemas.py StochasticResponse)
                  POST /api/v1/scenarios               시나리오 생성기 페이지 (page_scenarios.py)
응답이 없거나 계약이 아직 없는 패널은 목업으로 그린다. 서비스 응답을 쓴 패널에만 LIVE 태그를 단다.

TODO(통합): /forecast, /dispatch/milp 응답 계약이 정해지면 logic.sample_* 대신 파서를 붙인다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from streamlit.delta_generator import DeltaGenerator

import logic
import page_scenarios
from logic import API_PREFIX, ApiResult, Settings
from ui import (
    C_BRIGHT,
    C_GREEN,
    C_ORANGE,
    C_RED,
    C_TEXT,
    C_YELLOW,
    HEAT_SCALE,
    badge,
    compact,
    fetch_health,
    gauge_label,
    get_settings,
    inject_custom_css,
    kpi_badge,
    panel_title,
    pw,
    source_tag,
    style_dark,
    ton,
    won,
)

# 발전원 코드 → 색 (logic 목업 설비와 같은 코드)
FUEL_COLOR = {
    "nuclear": "#a3e635",
    "hydro": "#2dd4bf",
    "solar": "#facc15",
    "wind": "#86efac",
    "coal": "#6b7280",
    "lng": "#fb923c",
}
# collector generation_by_fuel 의 발전원 코드 → 표시 이름
COLLECTOR_FUEL_KO = {
    "nuclear": "원자력", "bituminous_coal": "유연탄", "anthracite": "국내탄", "lng": "가스",
    "oil": "유류", "hydro": "수력", "pumped": "양수", "solar": "태양광", "renewable": "신재생",
}
STATUS_COLOR = {"ok": C_GREEN, "warn": C_YELLOW, "critical": C_RED}
# 한 화면 배치를 위한 차트 높이 (1080p 기준 스크롤 없음)
H_MERIT, H_SMALL, H_GAUGE = 282, 188, 116
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


@st.cache_data(ttl=60, show_spinner=False)
def fetch_collector(base_url: str, name: str, limit: int) -> ApiResult:
    """collector 데이터셋의 최근 limit 행."""
    return logic.call_api("GET", f"{base_url}/data/{name}", params={"limit": limit}, timeout=5.0)


@st.cache_data(ttl=300, show_spinner="확률론적 최적화 호출 중…")
def fetch_stochastic(base_url: str, timeout: float) -> ApiResult:
    """POST /dispatch/stochastic. 풀이가 수 초 걸려 5분 캐시한다."""
    payload = {"start": f"{date.today().isoformat()}T00:00:00+09:00", "horizon_h": 24}
    return logic.call_api(
        "POST", f"{base_url}{API_PREFIX}/dispatch/stochastic", payload=payload, timeout=timeout
    )


def stochastic_result(ctx: Context) -> tuple[dict, bool, float | None]:
    """(VSS 요약, LIVE 여부, 지연시간). 서비스가 없거나 응답이 깨지면 목업."""
    res = (
        fetch_stochastic(ctx.urls["dispatch_api"], ctx.settings.timeout_s)
        if ctx.health["dispatch_api"].ok
        else ApiResult(error="dispatch_api down")
    )
    try:
        if res.ok:
            return logic.parse_stochastic(res.data), True, res.latency_s
    except (KeyError, TypeError, ValueError):
        pass
    return logic.parse_stochastic(logic.sample_stochastic()), False, None


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
    show_number: bool = True, height: int = 215, bar_color: str = C_GREEN,
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
                    "tickfont": {"size": 9, "color": C_TEXT},
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
        {"range": [warn, 30], "color": "rgba(163,230,53,.16)"},
    ]


def _melt_by_fuel(settings: Settings, df: pd.DataFrame) -> pd.DataFrame:
    """시간 x 발전원 긴 표 (hour, 발전원, mw)."""
    names = {f"{f.code}_mw": f.name for f in settings.fleet}
    melted = df.melt(id_vars=["hour"], value_vars=list(names), var_name="g", value_name="mw")
    melted["발전원"] = melted["g"].map(names)
    return melted


def _compact_chart(fig: go.Figure, height: int) -> go.Figure:
    """한 화면용 작은 차트: 범례는 위 오른쪽, 축 제목 없음, 여백 최소."""
    fig = style_dark(fig, height=height)
    fig.update_layout(
        margin=dict(l=4, r=4, t=22, b=4),
        legend=dict(orientation="h", x=1, xanchor="right", y=1.12, font=dict(size=10)),
    )
    fig.update_xaxes(title=None, tickfont=dict(size=10))
    fig.update_yaxes(title=None, tickfont=dict(size=10))
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


def render_adjust_controls(settings: Settings) -> logic.Adjust:
    """사이드바 what-if 조정 위젯. 모든 패널이 이 값으로 다시 계산된다."""
    defaults = _adjust_defaults(settings)
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)

    panel_title("시뮬레이션 조정")
    st.slider("수요 (%)", 80, 120, step=1, key="adj_demand",
              help="시간대별 수요 전체에 곱한다. 폭염 시나리오는 115%.")
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
            return "color:#a3e635;font-weight:600"
        if v.startswith("✗"):
            return "color:#f87171;font-weight:600"
        return ""

    st.dataframe(
        table.style.map(color_verdict, subset=["판정"]), hide_index=True, width="stretch"
    )


def card() -> DeltaGenerator:
    """한 화면용 카드. 같은 줄의 카드끼리 높이를 맞춘다."""
    return st.container(border=True, height="stretch")


def _gauge_card(
    title: str, value: str, unit: str, delta_html: str, sub: str, fig: go.Figure, key: str,
    value_color: str = C_GREEN,
) -> None:
    """수급 게이지 카드: 왼쪽 큰 값·변화, 오른쪽 게이지."""
    with card():
        html(f'<div class="vpp-kpi-head">{title}<small>실시간</small></div>')
        left, right = st.columns([1, 1.15], vertical_alignment="center")
        with left:
            html(
                f'<div class="vpp-kpi-value" style="color:{value_color}">{value}'
                f'<span class="u">{unit}</span></div>'
                f'<div class="vpp-kpi-line">{delta_html}</div>'
                f'<div class="vpp-kpi-sub">{sub}</div>'
            )
        with right:
            st.plotly_chart(fig, width="stretch", key=key, config=NO_BAR)


def render_supply_gauges(ctx: Context) -> None:
    """수요·공급·예비율 게이지 (기능 요구 10: 게이지로 표시)."""
    s, df, now, prev = ctx.settings, ctx.df, ctx.now, ctx.prev
    supply = sum(df[f"{f.code}_mw"] for f in s.fleet) + df.ess_discharge_mw
    sup_now, sup_prev = float(supply[now.name]), float(supply[prev.name])
    scale = 1000 if compact() else 1
    gmax = 110000 / scale

    c1, c2, c3 = st.columns(3)
    with c1:
        value, unit = pw(now.demand_mw).split(" ")
        _gauge_card(
            "전력 수요", value, unit, _delta(_pct_change(now.demand_mw, prev.demand_mw)),
            f"일 피크 {pw(df.demand_mw.max())}",
            _gauge_figure(now.demand_mw / scale, gmax, show_number=False, height=H_GAUGE),
            "g_demand",
        )
    with c2:
        value, unit = pw(sup_now).split(" ")
        _gauge_card(
            "공급 능력", value, unit, _delta(_pct_change(sup_now, sup_prev)),
            f"ESS 방전 {pw(now.ess_discharge_mw)} 포함",
            _gauge_figure(sup_now / scale, gmax, show_number=False, height=H_GAUGE,
                          bar_color=C_YELLOW),
            "g_supply",
        )
    with c3:
        label, css_class = logic.reserve_status(s, now.reserve_pct)
        color = STATUS_COLOR[css_class]
        _gauge_card(
            "예비율", f"{now.reserve_pct:.1f}", "%",
            _delta(now.reserve_pct - prev.reserve_pct, "%p")
            + f'<span class="vpp-alarm">{badge(label, css_class)}</span>',
            f'<span class="nw">Warning &lt;{s.reserve_warning_pct:g}%</span> · '
            f'<span class="nw">Critical &lt;{s.reserve_critical_pct:g}%</span>',
            _gauge_figure(now.reserve_pct, 30, steps=_reserve_steps(s), show_number=False,
                          height=H_GAUGE, bar_color=color),
            "g_reserve", value_color=color,
        )


def render_realtime_card(ctx: Context) -> None:
    """collector 실측 최신값 (수요·SMP)."""
    demand = fetch_collector(ctx.urls["collector"], "power_demand", 1)
    smp = fetch_collector(ctx.urls["collector"], "smp", 1)
    with card():
        html(f'<div class="vpp-kpi-head">KPX 실측{source_tag(demand.ok, demand.latency_s)}'
             "</div>")
        last_demand = logic.latest_row(demand) if demand.ok else None
        last_smp = logic.latest_row(smp) if smp.ok else None
        if not last_demand:
            html(f'<div class="vpp-kpi-sub vpp-empty">collector 응답 없음<br>'
                 f'{demand.error or "데이터 없음"}</div>')
            return
        rows = [("수요", pw(last_demand["demand_mw"]), last_demand["ts"])]
        if last_smp and last_smp.get("smp_won_per_kwh") is not None:
            rows.append(("SMP", f"{last_smp['smp_won_per_kwh']:,.1f} 원/kWh", last_smp["ts"]))
        html("".join(
            f'<div class="vpp-stat"><span>{k}</span><b>{v}</b><small>{ts}</small></div>'
            for k, v, ts in rows
        ))


def render_merit_order(ctx: Context) -> None:
    """시간대별 발전원 스택 (연료비 순) + 수요선."""
    s, df = ctx.settings, ctx.df
    fleet = sorted(s.fleet, key=lambda f: f.fuel_cost_won_per_kwh)
    with card():
        html('<div class="vpp-kpi-head">Merit Order 스택'
             f'<small>{" < ".join(f.name for f in fleet)} (연료비 순)</small></div>')
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
        st.plotly_chart(_compact_chart(fig, H_MERIT), width="stretch", key="c_merit",
                        config=NO_BAR)


def render_optimization(ctx: Context) -> None:
    """최적화 결과 — MILP 절감률·비용·응답·제약 6종 + Two-Stage VSS."""
    s = ctx.settings
    milp = logic.sample_dispatch_summary(ctx.df)
    vss, live, latency = stochastic_result(ctx)
    ok_saving = logic.meets(s, "milp_saving_pct", milp["saving_pct"])
    constraints = milp["constraints"]
    n_ok = sum(constraints.values())
    all_ok = n_ok == len(constraints)
    tip_rows = "".join(
        f'<span class="{"ok" if ok else "bad"}">{"✓" if ok else "✗"} {name}</span>'
        for name, ok in constraints.items()
    )
    with card():
        html(
            '<div class="vpp-kpi-head">최적화 결과<small>MILP · Two-Stage</small></div>'
            '<div class="vpp-kpi-row">'
            f'<span class="vpp-kpi-value" style="color:{C_GREEN if ok_saving else C_RED}">'
            f'{milp["saving_pct"]:.1f}<span class="u">%</span></span>'
            f'<span class="vpp-kpi-sub">Rule-based 대비 절감 '
            f'({logic.target_text(s, "milp_saving_pct")})</span></div>'
            f'<div class="vpp-stat"><span>MILP 연료비</span><b>{won(milp["milp_cost_won"])}</b>'
            f'<small>Rule {won(milp["rule_cost_won"])}</small></div>'
            f'<div class="vpp-stat"><span>API 응답</span><b>{milp["latency_s"]:.1f}초</b>'
            f'<small>{logic.target_text(s, "milp_latency_s")}</small></div>'
            f'<div class="vpp-stat"><span>VSS{source_tag(live, latency)}</span>'
            f'<b>{vss["vss_pct"]:.1f}%</b>'
            f'<small>{logic.target_text(s, "vss_pct")} · {vss["n_scenarios"]}개 시나리오</small>'
            "</div>"
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


def render_crisis_card(ctx: Context) -> None:
    """위기 시나리오 시뮬레이션 — 선택 시나리오 적용 결과 + 3종 하루 최저 예비율."""
    s = ctx.settings
    names = [sc.name for sc in s.crisis]
    with card():
        # 바로 아래가 위젯이라 Streamlit 마크다운의 음수 하단 여백(-1rem)을 메운다
        html('<div class="vpp-kpi-head" style="margin-bottom:1rem">위기 시나리오'
             "<small>예비율 ≥ 5% 유지</small></div>")
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
            v, ok = r["최저 예비율(%)"], bool(r["5% 유지"])
            color = C_GREEN if ok else C_RED
            rows.append(
                f'<div class="vpp-crisis-row"><span class="n">{r["시나리오"]}</span>'
                f'<span class="bar"><i style="width:{pos(v):.1f}%;background:{color}"></i>'
                f'<em style="left:{pos(crit):.1f}%"></em></span>'
                f'<b style="color:{color}">{v:.1f}%</b></div>'
            )
        html('<div class="vpp-crisis-cap">하루 중 최저 예비율 (세로선 = 5%)</div>'
             + "".join(rows))


def _period_x(ctx: Context) -> pd.Series:
    """기간 차트 x축: 하루면 시(0~23), 여러 날이면 시각."""
    return ctx.period.hour if ctx.days == 1 else ctx.period.ts


def _period_axis(fig: go.Figure, ctx: Context) -> go.Figure:
    """여러 날이면 x축을 날짜 눈금으로."""
    if ctx.days > 1:
        fig.update_xaxes(tickformat="%m/%d", dtick=86400000 * max(1, ctx.days // 7))
    return fig


def render_carbon(ctx: Context) -> None:
    """분석 기간의 시간대별 탄소 배출량 (ESS 반영 전/후)."""
    df, x = ctx.period, _period_x(ctx)
    total, base = df.carbon_ton.sum(), df.carbon_no_ess_ton.sum()
    diff = total - base
    with card():
        html(
            f'<div class="vpp-kpi-head">탄소 배출량 추이<small>{ctx.period_label}</small></div>'
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
    """분석 기간의 ESS 충/방전·SoC 와 피크 감소율 (기간 최대 피크 기준)."""
    s, df, x = ctx.settings, ctx.period, _period_x(ctx)
    before, after, cut_pct = logic.peak_reduction(df)
    ok = logic.meets(s, "ess_peak_cut_pct", cut_pct)
    with card():
        html(
            '<div class="vpp-kpi-head">ESS 충/방전 · SoC'
            f'<small>{ctx.period_label} · {logic.target_text(s, "ess_peak_cut_pct")}</small></div>'
            '<div class="vpp-kpi-row">'
            f'<span class="vpp-kpi-value sm" style="color:{C_GREEN if ok else C_RED}">'
            f'{cut_pct:.1f}%</span>'
            f'<span class="vpp-kpi-sub">피크 감소 {pw(before)} → {pw(after)}</span></div>'
        )
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_bar(x=x, y=df["ess_discharge_mw"], name="방전", marker_color=C_GREEN)
        fig.add_bar(x=x, y=-df["ess_charge_mw"], name="충전", marker_color=C_ORANGE)
        fig.add_scatter(
            x=x, y=df["ess_soc_pct"], name="SoC",
            line=dict(color=C_YELLOW, width=2, shape="spline"), secondary_y=True,
        )
        fig.update_traces(marker_line_width=0, marker_cornerradius=3, selector=dict(type="bar"))
        fig.update_layout(barmode="relative", bargap=0.2 if ctx.days == 1 else 0)
        fig = _period_axis(_compact_chart(fig, H_SMALL), ctx)
        fig.update_yaxes(range=[0, 100], secondary_y=True, gridcolor="rgba(0,0,0,0)",
                         showticklabels=False)
        st.plotly_chart(fig, width="stretch", key="c_ess", config=NO_BAR)


def render_generators(ctx: Context) -> None:
    """발전원 6종 — 현재 출력과 이용률 (목록형)."""
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
        html(f'<div class="vpp-kpi-head">발전원별 출력<small>{ctx.hour}시 · 이용률</small></div>'
             + "".join(rows))


def page_overview() -> None:
    """통합 관제 — 필수 항목을 스크롤 없이 한 화면에 둔다."""
    ctx = _ctx()
    render_adjust_notice(ctx)

    g, r = st.columns([3, 1])
    with g:
        render_supply_gauges(ctx)
    with r:
        render_realtime_card(ctx)

    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        render_merit_order(ctx)
    with c2:
        render_optimization(ctx)
    with c3:
        render_crisis_card(ctx)

    c4, c5, c6 = st.columns([1.15, 1.15, 0.9])
    with c4:
        render_carbon(ctx)
    with c5:
        render_ess(ctx)
    with c6:
        render_generators(ctx)


# ── 분석 상세 ────────────────────────────────────────────────────────────────


def render_forecast(settings: Settings) -> None:
    """168h 수요 예측 + 90% PI 와 예측 성능 지표."""
    fc = logic.sample_forecast(settings)
    metrics = logic.forecast_metrics(fc, settings)
    panel_title("수요 예측 · 90% 예측구간 (168h)")

    fig = go.Figure()
    fig.add_scatter(
        x=fc.hour, y=fc.demand_hi_mw, mode="lines", line=dict(width=0),
        showlegend=False, hoverinfo="skip",
    )
    fig.add_scatter(
        x=fc.hour, y=fc.demand_lo_mw, mode="lines", line=dict(width=0), fill="tonexty",
        fillcolor="rgba(163,230,53,.12)", name="90% PI",
    )
    fig.add_scatter(x=fc.hour, y=fc.demand_fc_mw, mode="lines", name="예측",
                    line=dict(color=C_GREEN, width=2.5, shape="spline"))
    fig.add_scatter(x=fc.hour, y=fc.demand_actual_mw, mode="lines", name="실측",
                    line=dict(color=C_ORANGE, width=1.6, shape="spline"))
    fig.add_vline(x=23.5, line=dict(color="rgba(255,255,255,.35)", dash="dash"))
    fig.update_layout(legend=dict(orientation="h", y=-0.2),
                      xaxis_title="예측 시점 (h ahead)", yaxis_title="MW")
    st.plotly_chart(style_dark(fig, height=300), width="stretch", key="c_forecast")

    html(
        kpi_badge(settings, "mape_24h_pct", "MAPE 24h", metrics["mape_24h_pct"])
        + kpi_badge(settings, "mape_168h_pct", "MAPE 168h", metrics["mape_168h_pct"])
        + kpi_badge(settings, "pi_coverage_pct", "PI Coverage", metrics["pi_coverage_pct"])
        + kpi_badge(settings, "nmae_solar_pct", "태양광 nMAE", metrics["nmae_solar_pct"])
        + kpi_badge(settings, "nmae_wind_pct", "풍력 nMAE", metrics["nmae_wind_pct"])
    )
    st.caption("nMAE 는 설비용량 기준 정규화. 점선 왼쪽이 24h ahead 구간.")


def render_optimization_detail(ctx: Context) -> None:
    """MILP 비용·제약과 Two-Stage RP/EV/EEV."""
    s = ctx.settings
    milp = logic.sample_dispatch_summary(ctx.df)
    vss, live, latency = stochastic_result(ctx)
    panel_title("최적화 상세 (MILP · Two-Stage)", source_tag(live, latency))
    c1, c2, c3 = st.columns(3)
    c1.metric("MILP 연료비 (일간)", won(milp["milp_cost_won"]))
    c2.metric("Rule-based 연료비", won(milp["rule_cost_won"]))
    c3.metric("불확실성 시나리오", f"{milp['n_scenarios']}개")
    c4, c5, c6 = st.columns(3)
    c4.metric("EV", won(vss["ev_won"]))
    c5.metric("EEV", won(vss["eev_won"]))
    c6.metric("RP", won(vss["rp_won"]))
    html(
        kpi_badge(s, "milp_saving_pct", "절감률", milp["saving_pct"])
        + kpi_badge(s, "milp_latency_s", "응답", milp["latency_s"])
        + kpi_badge(s, "vss_pct", "VSS", vss["vss_pct"])
    )
    st.caption(
        "VSS = EEV − RP, VSS% = VSS / EEV. EEV 는 평균 시나리오(EV) 해의 1단계를 고정하고 "
        "전 시나리오로 평가한 기대비용 (docs/formulation.md 9절)."
    )


def render_crisis_detail(ctx: Context) -> None:
    """시나리오 3종을 관측 시각에 적용한 게이지 + 하루 최저 예비율 표."""
    s = ctx.settings
    panel_title(f"위기 시나리오 상세 ({ctx.hour}시 적용)")
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
    """분석 기간의 발전원별 배출 내역·일별 요약과 Trade-off 설명."""
    panel_title(f"탄소 · ESS 분석 ({ctx.period_label})")
    st.dataframe(logic.carbon_by_fuel(ctx.period, ctx.settings), hide_index=True,
                 width="stretch")
    if ctx.days > 1:
        st.dataframe(
            logic.daily_summary(ctx.period), hide_index=True, width="stretch",
            column_config={"날짜": st.column_config.DateColumn(format="MM/DD (ddd)")},
        )
    ess = ctx.adjust.ess_spec(ctx.settings.ess)
    st.caption(
        "야간 충전분을 석탄(0.91)이 대고 저녁 방전이 LNG(0.45)를 대체하면 "
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
        render_forecast(ctx.settings)
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
    mix = fetch_collector(collector_url, "generation_by_fuel", 20)
    panel_title("KPX 발전원별 실측 출력 (collector)", source_tag(mix.ok, mix.latency_s))
    fuel_mw = logic.latest_mix(mix) if mix.ok else {}
    if not fuel_mw:
        st.caption(f"collector 응답 없음 ({mix.error or '데이터 없음'})")
        return
    mix_df = pd.DataFrame(
        {"발전원": [COLLECTOR_FUEL_KO.get(k, k) for k in fuel_mw], "MW": list(fuel_mw.values())}
    ).sort_values("MW")
    fig = px.bar(mix_df, x="MW", y="발전원", orientation="h")
    fig.update_traces(marker_color=C_GREEN, marker_line_width=0, marker_cornerradius=6)
    fig.update_yaxes(title=None)
    st.plotly_chart(style_dark(fig, height=260), width="stretch", key="c_mix")


def render_generation_heatmap(settings: Settings, df: pd.DataFrame) -> None:
    """발전원 x 시간 출력 히트맵."""
    panel_title("발전원별 시간대 출력 히트맵")
    order = [f.name for f in settings.fleet][::-1]
    pivot = _melt_by_fuel(settings, df).pivot(index="발전원", columns="hour", values="mw")
    fig = px.imshow(
        pivot.reindex(order), aspect="auto", color_continuous_scale=HEAT_SCALE,
        labels={"x": "시간", "y": "발전원", "color": "MW"},
    )
    fig.update_coloraxes(colorbar=dict(outlinewidth=0, tickfont=dict(color=C_TEXT, size=9)))
    st.plotly_chart(style_dark(fig, height=260), width="stretch", key="c_heatmap")


def render_hourly_table(settings: Settings, df: pd.DataFrame) -> None:
    """시간대 상세표와 CSV 내려받기."""
    panel_title("시간대 상세표")
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
        render_generation_heatmap(ctx.settings, ctx.df)
    with st.container(border=True):
        render_hourly_table(ctx.settings, ctx.df)


def main() -> None:
    """페이지 설정·테마를 한 번 적용하고 페이지를 띄운다."""
    st.set_page_config(page_title="VPP 통합 관제 대시보드", page_icon="⚡", layout="wide")
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
    ])
    # 사이드바 조정은 페이지 밖(여기)에서 그려야 페이지를 옮겨도 값이 유지된다.
    if nav.url_path != "scenarios":
        st.session_state["vpp_ctx"] = build_context()
    nav.run()


if __name__ == "__main__":
    main()
