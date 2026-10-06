"""대시보드 공용 UI — 테마 CSS, 차트 스타일, 배지, 단위 표시, 공용 캐시 호출.

app.py(통합 관제)와 page_scenarios.py(시나리오 생성기)가 같이 쓴다.
"""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

import logic
from logic import ApiResult, Settings

# 관제 화면 팔레트 (차콜 + 라임 그린)
C_GREEN = "#a3e635"
C_TEXT = "#9a9ca3"
C_BRIGHT = "#f4f5f6"
C_YELLOW = "#facc15"
C_ORANGE = "#fb923c"
C_RED = "#f87171"
C_CARD = "#232529"
HEAT_SCALE = [[0.0, "#232529"], [0.35, "#3f6212"], [0.7, "#84cc16"], [1.0, "#d9f99d"]]


@st.cache_resource
def get_settings() -> Settings:
    """config/dashboard.yaml. 컨테이너 수명 동안 한 번만 읽는다."""
    return logic.load_settings()


@st.cache_data(ttl=30, show_spinner=False)
def fetch_health(base_url: str) -> ApiResult:
    """서비스 /health. 꺼져 있을 때 화면이 오래 멈추지 않게 타임아웃을 짧게 둔다."""
    return logic.call_api("GET", f"{base_url}/health", timeout=2.0)


# ── 스타일 (차콜 배경 + 둥근 카드 + 라임 포인트) ────────────────────────────


def inject_custom_css() -> None:
    """관제 화면 테마 CSS 를 주입한다."""
    st.markdown(
        """
        <style>
        @import url("https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable.min.css");
        :root {
            --vpp-bg: #17181c; --vpp-card: #232529; --vpp-card-2: #2b2d32;
            --vpp-line: rgba(255,255,255,.07); --vpp-green: #a3e635;
            --vpp-yellow: #facc15; --vpp-red: #f87171;
            --vpp-text: #f4f5f6; --vpp-muted: #9a9ca3;
        }
        html, body, .stApp, [class*="st-"], button, input, textarea, select {
            font-family: "Pretendard Variable", Pretendard, -apple-system, "Segoe UI",
                         "Malgun Gothic", sans-serif;
        }
        /* 아이콘 폰트는 덮어쓰지 않는다 */
        [data-testid="stIconMaterial"], .material-symbols-rounded {
            font-family: "Material Symbols Rounded" !important;
        }

        .stApp { background: var(--vpp-bg); }
        header[data-testid="stHeader"] { background: transparent !important; }
        div[data-testid="stDecoration"] { display: none; }
        div[data-testid="stToolbar"] { right: .6rem; }

        /* 한 화면 배치: 위 여백·카드 간격을 줄인다 */
        .block-container { padding: 1.4rem 1.6rem 1rem; max-width: 1680px; }
        /* 카드 사이 간격 (가로·세로) — 카드 안쪽 배치 간격은 아래에서 따로 좁게 둔다 */
        div[data-testid="stMainBlockContainer"] div[data-testid="stVerticalBlock"] { gap: 1.1rem; }
        div[data-testid="stHorizontalBlock"] { gap: 1.1rem; }
        div[data-testid="stLayoutWrapper"] > div[data-testid="stVerticalBlock"]
            div[data-testid="stHorizontalBlock"] { gap: .75rem; }

        /* 사이드바: 둥근 카드형 */
        section[data-testid="stSidebar"] {
            background: var(--vpp-card); border-right: none;
        }
        section[data-testid="stSidebar"] .vpp-section-title { color: var(--vpp-muted); }
        /* 네비게이션: 활성 항목은 라임 텍스트 + 왼쪽 바 */
        a[data-testid="stSidebarNavLink"] {
            border-radius: 10px; padding: .45rem .7rem; margin: .1rem 0;
        }
        a[data-testid="stSidebarNavLink"]:hover { background: var(--vpp-card-2); }
        a[data-testid="stSidebarNavLink"][aria-current="page"] {
            background: rgba(163,230,53,.12); box-shadow: inset 3px 0 0 var(--vpp-green);
        }
        a[data-testid="stSidebarNavLink"][aria-current="page"] span {
            color: var(--vpp-green) !important; font-weight: 600;
        }

        /* 패널: 차콜 카드 + 큰 라운드. st.container(border=True) 는
           stLayoutWrapper 바로 아래 stVerticalBlock 으로 렌더된다 (Streamlit 1.64). */
        div[data-testid="stLayoutWrapper"] > div[data-testid="stVerticalBlock"] {
            background: var(--vpp-card);
            border: none !important;
            border-radius: 18px;
            padding: .85rem 1.05rem;
            gap: .35rem !important;
        }
        /* height="stretch" 카드는 높이가 고정된 flex 열이라 내용이 눌리지 않게 한다 */
        div[data-testid="stLayoutWrapper"] > div[data-testid="stVerticalBlock"] > div {
            flex-shrink: 0 !important;
        }

        /* 상단 바: 제목 · 부제 (분석 상세 · 시간대 데이터 · 시나리오 생성기) */
        .vpp-topbar {
            display: flex; align-items: center; gap: .9rem; flex-wrap: wrap;
            padding-right: 6rem;   /* Streamlit 툴바 자리 */
        }
        .vpp-title {
            font-size: 1.45rem; font-weight: 700; color: var(--vpp-text); letter-spacing: -.01em;
        }
        .vpp-title .vpp-logo { color: var(--vpp-green); margin-right: .35rem; }
        .vpp-subtitle { color: var(--vpp-muted); font-size: .8rem; }

        /* 예비율 알람 배지 (Warning / Critical) — Critical 이면 깜박인다 */
        .vpp-alarm .vpp-badge.critical { animation: vpp-blink 1.6s ease-in-out infinite; }
        @keyframes vpp-blink { 0%,100% { opacity: 1; } 50% { opacity: .62; } }

        /* 뱃지: 알약형 */
        .vpp-badge {
            display: inline-flex; align-items: center; gap: .35rem;
            padding: .16rem .7rem; border-radius: 999px; margin: .14rem .2rem .14rem 0;
            font-size: .74rem; font-weight: 600; line-height: 1.6;
        }
        .vpp-badge.ok { background: rgba(163,230,53,.14); color: var(--vpp-green); }
        .vpp-badge.warn { background: rgba(250,204,21,.14); color: var(--vpp-yellow); }
        .vpp-badge.critical { background: rgba(248,113,113,.15); color: var(--vpp-red); }

        /* 데이터 출처 태그 (패널 제목 오른쪽) */
        .vpp-src {
            font-size: .64rem; font-weight: 700; letter-spacing: .04em; margin-left: .5rem;
            padding: .08rem .5rem; border-radius: 999px; vertical-align: middle;
        }
        .vpp-src.live { color: #17181c; background: var(--vpp-green); }

        /* 카드 머리글·큰 값 */
        .vpp-kpi-head {
            display: flex; justify-content: space-between; align-items: center; gap: .5rem;
            font-size: .95rem; font-weight: 600; color: var(--vpp-text);
            margin-bottom: .65rem;  /* 제목과 내용 사이 */
        }
        .vpp-kpi-head small {
            font-size: .7rem; color: var(--vpp-muted); font-weight: 500; text-align: right;
        }
        .vpp-kpi-row {
            display: flex; align-items: baseline; gap: .55rem; flex-wrap: wrap; margin-top: .3rem;
        }
        .vpp-kpi-value {
            font-size: 1.85rem; font-weight: 700; color: var(--vpp-green);
            line-height: 1.05; letter-spacing: -.01em; white-space: nowrap;
        }
        .vpp-kpi-value.sm { font-size: 1.4rem; }
        .vpp-kpi-value .u { font-size: .9rem; margin-left: .2rem; }
        .vpp-kpi-line { display: flex; align-items: center; gap: .4rem; margin-top: .3rem; }
        .vpp-kpi-delta { font-size: .76rem; color: var(--vpp-text); white-space: nowrap; }
        .vpp-kpi-delta.up::before { content: "↑ "; color: var(--vpp-green); }
        .vpp-kpi-delta.down::before { content: "↓ "; color: var(--vpp-red); }
        .vpp-kpi-sub { font-size: .72rem; color: var(--vpp-muted); margin-top: .2rem; }
        .nw { white-space: nowrap; }
        .vpp-empty { margin-top: .8rem; line-height: 1.6; }

        /* 가로 불릿 게이지 (수요·공급능력·예비율) */
        .vpp-bullet { margin: .85rem 0 .1rem; }
        .vpp-bullet .track {
            position: relative; height: 14px; border-radius: 999px; overflow: hidden;
            background: rgba(255,255,255,.07);
        }
        .vpp-bullet .zone { position: absolute; top: 0; bottom: 0; }
        .vpp-bullet .fill {
            position: absolute; left: 0; top: 3px; bottom: 3px; border-radius: 999px;
        }
        .vpp-bullet .mark {
            position: absolute; top: 0; bottom: 0; width: 3px; border-radius: 2px;
            background: var(--vpp-text); transform: translateX(-50%);
        }
        .vpp-bullet .stripe {
            position: absolute; top: 3px; bottom: 3px; border-radius: 0 999px 999px 0;
            background: repeating-linear-gradient(135deg,
                rgba(23,24,28,.55) 0 3px, transparent 3px 6px);
        }
        .vpp-bullet .ticks {
            position: relative; height: 1.1rem; margin-top: .3rem;
            font-size: .74rem; color: #c3c5ca;
        }
        .vpp-bullet .ticks span { position: absolute; transform: translateX(-50%); }
        .vpp-bullet .ticks span:first-child { transform: none; }
        .vpp-bullet .ticks span:last-child { transform: translateX(-100%); }
        /* 불릿 게이지 아래 설명 (예비력 · 예비율 기준 범례) */
        .vpp-bullet-sub {
            margin: .35rem 0 .7rem; font-size: .82rem; color: var(--vpp-text);
        }
        .vpp-bullet-sub b { color: var(--vpp-yellow); font-weight: 700; }
        .vpp-key {
            display: inline-block; width: 22px; height: 10px; margin-right: .45rem;
            border-radius: 999px; vertical-align: -1px;
        }
        .vpp-key.stripe {
            background: repeating-linear-gradient(135deg,
                rgba(23,24,28,.55) 0 3px, transparent 3px 6px), var(--vpp-yellow);
        }
        .vpp-legend { display: flex; flex-wrap: wrap; gap: .25rem .9rem; }
        .vpp-legend span { white-space: nowrap; }
        .vpp-legend i {
            display: inline-block; width: 9px; height: 9px; margin-right: .35rem;
            border-radius: 50%; vertical-align: 0;
        }

        /* KPX 실측: 값 타일 + 갱신 주기 태그 */
        .vpp-tile {
            display: flex; justify-content: space-between; align-items: baseline;
            padding: .55rem .8rem; border-radius: 12px; background: var(--vpp-card-2);
        }
        .vpp-tile + .vpp-tile { margin-top: .45rem; }
        .vpp-tile span { font-size: .84rem; color: var(--vpp-muted); font-weight: 600; }
        .vpp-tile b { font-size: 1.45rem; font-weight: 700; line-height: 1.1; }
        .vpp-tile b small {
            font-size: .78rem; margin-left: .25rem; color: var(--vpp-muted); font-weight: 600;
        }
        .vpp-sync {
            display: inline-flex; align-items: center; gap: .35rem;
            font-size: .7rem; font-weight: 600; padding: .12rem .55rem; border-radius: 999px;
        }
        .vpp-sync::before {
            content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor;
        }
        .vpp-sync.ok { color: var(--vpp-green); background: rgba(163,230,53,.12); }
        .vpp-sync.ok::before { animation: vpp-blink 1.6s ease-in-out infinite; }
        .vpp-sync.off { color: var(--vpp-red); background: rgba(248,113,113,.14); }

        /* 최적화 결과: 큰 절감률 + 기준 칩 + 값 타일 */
        .vpp-hero-sub {
            display: inline-flex; align-items: center; gap: .45rem; flex-wrap: wrap;
            font-size: .8rem; color: var(--vpp-text);
        }
        .vpp-tiles { margin-top: .5rem; }
        .vpp-tiles .vpp-tile + .vpp-tile { margin-top: .65rem; }  /* 타일 사이 간격 */
        .vpp-tile.opt { align-items: center; padding: .45rem .75rem; }
        .vpp-tile.opt > div { display: flex; flex-direction: column; gap: .05rem; }
        .vpp-tile.opt > div:last-child { align-items: flex-end; }
        .vpp-tile.opt small { font-size: .7rem; color: var(--vpp-muted); }
        .vpp-tile.opt b { font-size: 1.15rem; color: var(--vpp-text); }
        .vpp-chk {
            font-style: normal; font-size: .68rem; font-weight: 700; white-space: nowrap;
            padding: .08rem .5rem; border-radius: 999px;
        }
        .vpp-chk.ok { color: var(--vpp-green); background: rgba(163,230,53,.14); }
        .vpp-chk.bad { color: var(--vpp-red); background: rgba(248,113,113,.15); }

        /* 이름 · 값 · 보조 한 줄 */
        .vpp-stat {
            display: grid; grid-template-columns: auto 1fr; align-items: baseline;
            column-gap: .6rem; padding: .42rem 0;
            font-size: .8rem;
        }
        .vpp-stat span { color: var(--vpp-muted); }
        .vpp-stat b { color: var(--vpp-text); text-align: right; font-size: .92rem; }
        .vpp-stat small {
            grid-column: 1 / -1; color: var(--vpp-muted); font-size: .68rem; text-align: right;
        }
        .vpp-kpi-head + .vpp-stat { margin-top: .5rem; }
        /* 남은 카드 높이를 채우고 항목을 고르게 배치 (KPX 실측) */
        div[data-testid="stElementContainer"]:has(.vpp-stats-fill) {
            flex: 1 1 auto !important; padding-bottom: 1rem;  /* 마크다운 음수 여백 보정 */
        }
        div[data-testid="stElementContainer"]:has(.vpp-stats-fill) div:has(.vpp-stats-fill) {
            height: 100%;
        }
        .vpp-stats-fill {
            height: 100%; display: flex; flex-direction: column; justify-content: space-around;
        }

        /* MILP 제약 6종: 카드 왼쪽 아래 체크 아이콘, 마우스를 올리면 목록 */
        div[data-testid="stElementContainer"]:has(.vpp-checktip) {
            margin-top: auto; padding-bottom: .75rem;  /* 마크다운 음수 하단 여백 보정 */
        }
        .vpp-checktip {
            position: relative; display: inline-flex; align-items: center; gap: .45rem;
            font-size: .72rem; color: var(--vpp-muted); cursor: help; outline: none;
        }
        .vpp-checktip .ic {
            width: 22px; height: 22px; border-radius: 50%; display: inline-grid;
            place-items: center; font-size: .78rem; font-weight: 700;
        }
        .vpp-checktip.ok .ic { background: rgba(163,230,53,.16); color: var(--vpp-green); }
        .vpp-checktip.bad .ic { background: rgba(248,113,113,.16); color: var(--vpp-red); }
        .vpp-checktip .tip {
            position: absolute; left: 0; bottom: calc(100% + 8px); z-index: 20;
            display: grid; gap: .3rem; min-width: 180px; padding: .65rem .8rem;
            border-radius: 12px; background: var(--vpp-card-2); color: var(--vpp-text);
            box-shadow: 0 10px 28px rgba(0,0,0,.5); font-size: .74rem;
            opacity: 0; visibility: hidden; transform: translateY(4px);
            transition: opacity .15s, transform .15s, visibility .15s;
        }
        .vpp-checktip .tip b { font-size: .76rem; margin-bottom: .1rem; }
        .vpp-checktip .tip .ok { color: var(--vpp-green); }
        .vpp-checktip .tip .bad { color: var(--vpp-red); }
        .vpp-checktip:hover .tip, .vpp-checktip:focus .tip {
            opacity: 1; visibility: visible; transform: none;
        }

        /* 위기 시나리오 행 */
        .vpp-crisis-cap { font-size: .68rem; color: var(--vpp-muted); margin: .55rem 0 .2rem; }
        .vpp-crisis-row {
            display: grid; grid-template-columns: 1fr 70px 3.2rem; align-items: center;
            gap: .5rem; padding: .3rem 0; font-size: .74rem; color: var(--vpp-text);
        }
        .vpp-crisis-row .n { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .vpp-crisis-row b { text-align: right; }

        /* 가로 진행 막대 (위기 시나리오·발전원 공용) */
        .vpp-crisis-row .bar, .vpp-gen-row .bar {
            position: relative; height: 7px; border-radius: 999px;
            background: rgba(255,255,255,.08);
        }
        .vpp-crisis-row .bar i, .vpp-gen-row .bar i {
            display: block; height: 100%; border-radius: 999px;
        }
        .vpp-crisis-row .bar em {
            position: absolute; top: -3px; width: 2px; height: 13px;
            background: var(--vpp-text); transform: translateX(-50%);
        }

        /* 발전원 목록 */
        .vpp-gen-row {
            display: grid; grid-template-columns: 18px 3.4rem 4.2rem 1fr 2.2rem;
            align-items: center; gap: .5rem; padding: .36rem 0;
            font-size: .76rem;
        }
        .vpp-kpi-head + .vpp-gen-row { margin-top: .45rem; }
        .vpp-gen-row .n { color: var(--vpp-text); }
        .vpp-gen-row b { color: var(--vpp-text); text-align: right; white-space: nowrap; }
        .vpp-gen-row small { color: var(--vpp-muted); text-align: right; }

        /* 게이지 라벨 (plotly 밖이라 폭 변화에도 안 잘림) */
        .vpp-gauge-label {
            text-align: center; font-size: .78rem; color: var(--vpp-muted);
            font-weight: 600; margin: .1rem 0 -.5rem; line-height: 1.35;
        }

        /* 패널 제목 */
        .vpp-section-title {
            font-size: 1rem; font-weight: 600; color: var(--vpp-text);
            margin: .05rem 0 .6rem;
        }

        /* 메트릭: 작은 내부 카드 */
        div[data-testid="stMetric"] {
            background: var(--vpp-card-2); border-radius: 12px; padding: .55rem .8rem;
        }
        div[data-testid="stMetricLabel"] {
            font-size: .76rem; color: var(--vpp-muted); font-weight: 600;
        }
        div[data-testid="stMetricValue"] {
            font-size: 1.35rem; font-weight: 700; color: var(--vpp-green);
        }

        /* 버튼 */
        div[data-testid="stButton"] button, div[data-testid="stDownloadButton"] button {
            border-radius: 10px; border: 1px solid var(--vpp-line);
            background: var(--vpp-card-2);
        }
        div[data-testid="stDownloadButton"] button,
        div[data-testid="stButton"] button[kind="primary"],
        div[data-testid="stFormSubmitButton"] button[kind="primaryFormSubmit"] {
            background: var(--vpp-green); color: #17181c; border: none; font-weight: 600;
        }
        div[data-testid="stButton"] button[kind="primary"] p,
        div[data-testid="stFormSubmitButton"] button p { color: #17181c; }

        /* 표·확장 패널 */
        div[data-testid="stDataFrame"] { border-radius: 12px; overflow: hidden; }
        div[data-testid="stExpander"] details {
            border-radius: 12px; border-color: var(--vpp-line);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def style_dark(fig: go.Figure, height: int = 300) -> go.Figure:
    """모든 plotly 차트에 공통 관제 테마를 적용한다."""
    fig.update_layout(
        height=height,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=C_TEXT, size=11, family="Pretendard Variable, Malgun Gothic, sans-serif"),
        margin=dict(l=12, r=12, t=28, b=12),
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
        hoverlabel=dict(
            bgcolor="#2b2d32", bordercolor="rgba(255,255,255,.18)", font_color=C_BRIGHT
        ),
        bargap=0.35,
    )
    axis = dict(
        gridcolor="rgba(255,255,255,.06)",
        zerolinecolor="rgba(255,255,255,.12)",
        linecolor="rgba(0,0,0,0)",
    )
    fig.update_xaxes(**axis)
    fig.update_yaxes(**axis)
    return fig


# ── 작은 HTML 조각 ───────────────────────────────────────────────────────────


def badge(label: str, css_class: str) -> str:
    """상태 배지 HTML. css_class 는 ok / warn / critical."""
    return f'<span class="vpp-badge {css_class}">{label}</span>'


def kpi_badge(settings: Settings, key: str, label: str, value: float) -> str:
    """성능 기준 배지. 기준 충족이면 초록, 미달이면 빨강."""
    ok = logic.meets(settings, key, value)
    unit = "초" if key.endswith("_s") else "%"
    mark = "✓" if ok else "✗"
    text = f"{label} {value:.1f}{unit} ({logic.target_text(settings, key)}) {mark}"
    return badge(text, "ok" if ok else "critical")


def source_tag(live: bool, latency_s: float | None = None) -> str:
    """패널 제목 옆 LIVE 태그 (서비스 응답일 때만, 지연시간 포함)."""
    if not live:
        return ""
    suffix = f" · {latency_s:.2f}s" if latency_s is not None else ""
    return f'<span class="vpp-src live">LIVE{suffix}</span>'


def panel_title(text: str, tag: str = "") -> None:
    """패널 제목. tag 에는 source_tag() 결과를 넘긴다."""
    st.markdown(f'<div class="vpp-section-title">{text}{tag}</div>', unsafe_allow_html=True)


def gauge_label(text: str) -> None:
    """게이지 위 라벨. plotly 밖(HTML)이라 차트 폭이 변해도 잘리지 않는다."""
    st.markdown(f'<div class="vpp-gauge-label">{text}</div>', unsafe_allow_html=True)


def compact() -> bool:
    """큰 단위(GW·억원·천t) 보기 여부."""
    return st.session_state.get("compact_units", True)


def pw(mw: float) -> str:
    """전력 표시 (보기 설정 반영)."""
    return logic.fmt_power(mw, compact())


def won(value: float) -> str:
    """금액 표시 (보기 설정 반영)."""
    return logic.fmt_won(value, compact())


def ton(value: float) -> str:
    """배출량 표시 (보기 설정 반영)."""
    return logic.fmt_ton(value, compact())
