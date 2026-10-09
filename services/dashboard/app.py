"""dashboard — 통합 관제 대시보드 (실데이터 연동 전 UI 목업).

CLAUDE.md 요구사항 10번(통합 관제 대시보드)과 최종 결과물 4번을 기준으로
화면 골격만 먼저 구성한다. 모든 수치는 더미 데이터이며, 실제로는
FORECAST_URL(forecast_api)·DISPATCH_URL(dispatch_api)을 REST로 호출해
shared/schemas.py 계약에 맞는 응답을 받아와야 한다.

디자인: 다크 네이비 + 시안 네온 + 코너 브래킷 패널(관제 대시보드 관례).
요구사항의 "한 화면에 표시"를 지키기 위해 탭 없이 단일 스크롤 페이지로 배치한다.

TODO(통합): mock_* 함수를 requests.get/post(FORECAST_URL·DISPATCH_URL) 호출로 교체.
    현재는 shared/를 import하지 않는다 — Dockerfile이 app.py 한 파일만 COPY하는
    구조라(services/dashboard 디렉터리가 빌드 컨텍스트) shared/는 빌드 이미지에
    없다. 실연동 시점에 빌드 컨텍스트를 레포 루트로 바꾸거나 스키마를 복제해야 한다.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

# ── 상수 (CLAUDE.md 성능 기준 §63, 발전원 파라미터 §92 기준) ────────────────
RESERVE_WARNING_PCT = 7.0
RESERVE_CRITICAL_PCT = 5.0

# 급전 우선순위(연료비 오름차순)
GENERATOR_ORDER = ["원자력", "수력", "태양광", "풍력", "석탄", "LNG"]
GENERATOR_COLOR = {
    "원자력": "#22d3ee",
    "수력": "#3b82f6",
    "태양광": "#fde047",
    "풍력": "#4ade80",
    "석탄": "#64748b",
    "LNG": "#f472b6",
}
CO2_TON_PER_MWH = {
    "원자력": 0.0, "수력": 0.0, "태양광": 0.0,
    "풍력": 0.0, "석탄": 0.91, "LNG": 0.45,
}
# 설비용량 (CLAUDE.md §92 EPSIS 참조표)
CAPACITY_MW = {
    "원자력": 24000, "수력": 6500, "석탄": 28000,
    "LNG": 32000, "태양광": 12000, "풍력": 3500,
}
ESS_POWER_MW = 100  # config/ess.yaml power_mw=0(플레이스홀더) 대신 화면 시연용 임시값

# 관제 화면 팔레트
C_CYAN = "#22d3ee"
C_TEXT = "#a9c8e0"
C_BRIGHT = "#e8f6ff"
C_VIOLET = "#a78bfa"
C_PINK = "#f472b6"
C_YELLOW = "#fde047"
HEAT_SCALE = [[0.0, "#08213a"], [0.35, "#0e7490"], [0.7, "#22d3ee"], [1.0, "#a5f3fc"]]

# 발전원 아이콘 (인라인 SVG — 외부 이미지 파일 의존 없음)
_ICON_SVG = {
    "원자력": (
        '<circle cx="12" cy="12" r="2.1" fill="{c}" stroke="none"/>'
        '<ellipse cx="12" cy="12" rx="9.6" ry="4.1"/>'
        '<ellipse cx="12" cy="12" rx="9.6" ry="4.1" transform="rotate(60 12 12)"/>'
        '<ellipse cx="12" cy="12" rx="9.6" ry="4.1" transform="rotate(120 12 12)"/>'
    ),
    "수력": (
        '<path d="M12 2.8s-6.6 7.3-6.6 11.3a6.6 6.6 0 0 0 13.2 0C18.6 10.1 12 2.8 12 2.8Z"/>'
        '<path d="M8.6 14.6c1.1-1 2.3-1 3.4 0s2.3 1 3.4 0"/>'
    ),
    "태양광": (
        '<circle cx="12" cy="12" r="4"/>'
        '<path d="M12 1.8v2.6M12 19.6v2.6M1.8 12h2.6M19.6 12h2.6'
        'M4.8 4.8l1.9 1.9M17.3 17.3l1.9 1.9M19.2 4.8l-1.9 1.9M6.7 17.3l-1.9 1.9"/>'
    ),
    "풍력": (
        '<path d="M12 13.1V21.6M9.4 21.6h5.2"/>'
        '<circle cx="12" cy="11.6" r="1.5" fill="{c}" stroke="none"/>'
        '<path d="M12 10.1V2.6M13.4 12.4l6.4 3.7M10.6 12.4l-6.4 3.7"/>'
    ),
    "석탄": (
        '<path d="M2.6 20.6v-8.2l5.4 3.1v-3.1l5.4 3.1v-3.1l5.4 3.1v5.1Z"/>'
        '<path d="M6.2 9.2V5.6h2.8v4.9"/>'
    ),
    "LNG": (
        '<path d="M12 21.4c3.5 0 6.1-2.4 6.1-5.7 0-3.5-2.6-5.3-3.8-8.2-.6 2.1-1.7 2.9-2.7 3.7'
        ".2-2.6-.8-5.3-2.6-7.4-.4 2.9-2.3 4.3-3.5 6.4-.8 1.5-1.2 3.1-1.2 4.9 0 3.5 2.7 6.3 "
        '7.7 6.3Z"/>'
    ),
}

CRISIS_SCENARIOS = {
    "없음": {},
    "폭염 (수요 +15%)": {"demand_mult": 1.15},
    "발전소 탈락 (-1,400MW)": {"capacity_delta_mw": -1400},
    "재생에너지 램프다운 (태양광 -50%)": {"solar_mult": 0.5},
}

# 하루 수요 곡선(전형적인 한국 부하 패턴을 본뜬 토이 값, MW)
_DEMAND_SHAPE_MW = [
    52000, 50000, 49000, 48000, 49000, 52000,
    58000, 66000, 72000, 76000, 79000, 81000,
    82000, 80000, 78000, 77000, 78000, 80000,
    84000, 86000, 83000, 74000, 65000, 57000,
]
_SOLAR_SHAPE_MW = [
    0, 0, 0, 0, 0, 200,
    1200, 3600, 6400, 8800, 10400, 11200,
    11600, 11200, 10000, 8000, 5600, 2800,
    800, 100, 0, 0, 0, 0,
]


@dataclass
class HourRow:
    hour: int
    demand_mw: float
    nuclear_mw: float
    hydro_mw: float
    solar_mw: float
    wind_mw: float
    coal_mw: float
    lng_mw: float
    ess_charge_mw: float
    ess_discharge_mw: float
    ess_soc_pct: float
    carbon_ton: float
    available_capacity_mw: float

    @property
    def reserve_pct(self) -> float:
        return (self.available_capacity_mw - self.demand_mw) / self.demand_mw * 100


# ── 더미 데이터 생성 ────────────────────────────────────────────────────────
@st.cache_data
def mock_timeseries(seed: int = 42) -> pd.DataFrame:
    """24시간 더미 급전 결과. dispatch_api MILP 응답이 연결되면 제거."""
    rng = random.Random(seed)
    soc = 30.0  # %
    rows: list[HourRow] = []

    for h in range(24):
        demand = _DEMAND_SHAPE_MW[h] * (1 + rng.uniform(-0.015, 0.015))
        solar = _SOLAR_SHAPE_MW[h] * (1 + rng.uniform(-0.05, 0.05)) if _SOLAR_SHAPE_MW[h] else 0.0
        wind = 800 + 700 * math.sin(h / 24 * 4 * math.pi) + rng.uniform(-300, 300)
        wind = max(wind, 0.0)

        # ESS: 야간(00~05시, 23시) 충전, 저녁 피크(18~20시) 방전
        ess_charge = ESS_POWER_MW if h in (0, 1, 2, 3, 4, 5, 23) else 0.0
        ess_discharge = ESS_POWER_MW if h in (18, 19, 20) else 0.0
        soc = min(max(soc + ess_charge * 0.85 / 4 - ess_discharge / 0.85 / 4, 0.0), 100.0)

        # Merit Order 급전: 원자력(기저) → 재생(must-run) → 석탄 → LNG → 수력(첨두)
        # 수력은 값은 싸지만 에너지 제약이 있어 양수발전처럼 첨두에 투입한다고 가정.
        # ESS 방전은 발전기가 맡을 부하를 덜어주고, 충전은 부하로 더해진다.
        nuclear = CAPACITY_MW["원자력"] * 0.95
        hydro_base = CAPACITY_MW["수력"] * 0.12  # 유입식 기저 출력

        net_demand = demand + ess_charge - ess_discharge
        remaining = max(net_demand - (nuclear + hydro_base + solar + wind), 0.0)
        coal = min(remaining, CAPACITY_MW["석탄"])
        remaining -= coal
        lng = min(remaining, CAPACITY_MW["LNG"])
        remaining -= lng
        hydro = hydro_base + min(remaining, CAPACITY_MW["수력"] - hydro_base)

        carbon = coal * CO2_TON_PER_MWH["석탄"] + lng * CO2_TON_PER_MWH["LNG"]

        available_capacity = (
            CAPACITY_MW["원자력"] + CAPACITY_MW["수력"] + CAPACITY_MW["석탄"] + CAPACITY_MW["LNG"]
            + solar + wind + ESS_POWER_MW
        )

        rows.append(
            HourRow(
                hour=h, demand_mw=demand, nuclear_mw=nuclear, hydro_mw=hydro,
                solar_mw=solar, wind_mw=wind, coal_mw=coal, lng_mw=lng,
                ess_charge_mw=ess_charge, ess_discharge_mw=ess_discharge, ess_soc_pct=soc,
                carbon_ton=carbon, available_capacity_mw=available_capacity,
            )
        )
    return pd.DataFrame([r.__dict__ | {"reserve_pct": r.reserve_pct} for r in rows])


def apply_scenario(row: pd.Series, scenario_key: str) -> tuple[float, float]:
    """선택한 위기 시나리오를 (수요, 가용용량)에 반영해 반환한다."""
    cfg = CRISIS_SCENARIOS[scenario_key]
    demand = row.demand_mw * cfg.get("demand_mult", 1.0)
    capacity = row.available_capacity_mw + cfg.get("capacity_delta_mw", 0.0)
    if "solar_mult" in cfg:
        capacity -= row.solar_mw * (1 - cfg["solar_mult"])
    return demand, capacity


# ── 스타일 (다크 네이비 + 시안 네온 + 코너 브래킷) ──────────────────────────
def inject_custom_css() -> None:
    st.markdown(
        """
        <style>
        :root { --vpp-corner: #22d3ee; }

        /* 배경: 딥 네이비 + 상단 시안 글로우 */
        .stApp {
            background:
                radial-gradient(1100px 520px at 50% -8%, rgba(34,211,238,.10), transparent 62%),
                linear-gradient(180deg, #05101d 0%, #0a1c30 45%, #05101d 100%);
        }
        /* 미세 그리드 텍스처 */
        .stApp::before {
            content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 0;
            background-image:
                linear-gradient(rgba(34,211,238,.030) 1px, transparent 1px),
                linear-gradient(90deg, rgba(34,211,238,.030) 1px, transparent 1px);
            background-size: 52px 52px, 52px 52px;
        }
        /* Streamlit 기본 상단 바 — 배경 그라데이션이 그대로 비치도록 투명 처리 */
        header[data-testid="stHeader"] { background: transparent !important; }
        div[data-testid="stDecoration"] { display: none; }
        div[data-testid="stToolbar"] { right: .6rem; }

        .block-container { padding-top: 3.4rem; max-width: 1500px; }

        /* 패널: 반투명 + 4모서리 코너 브래킷 */
        div[data-testid="stVerticalBlockBorderWrapper"] {
            position: relative;
            background-color: rgba(11, 32, 56, .55) !important;
            border: 1px solid rgba(34,211,238,.10) !important;
            border-radius: 3px !important;
            background-image:
                linear-gradient(var(--vpp-corner), var(--vpp-corner)),
                linear-gradient(var(--vpp-corner), var(--vpp-corner)),
                linear-gradient(var(--vpp-corner), var(--vpp-corner)),
                linear-gradient(var(--vpp-corner), var(--vpp-corner)),
                linear-gradient(var(--vpp-corner), var(--vpp-corner)),
                linear-gradient(var(--vpp-corner), var(--vpp-corner)),
                linear-gradient(var(--vpp-corner), var(--vpp-corner)),
                linear-gradient(var(--vpp-corner), var(--vpp-corner));
            background-repeat: no-repeat;
            background-size:
                15px 2px, 2px 15px, 15px 2px, 2px 15px,
                15px 2px, 2px 15px, 15px 2px, 2px 15px;
            background-position:
                left top, left top, right top, right top,
                left bottom, left bottom, right bottom, right bottom;
        }
        div[data-testid="stVerticalBlockBorderWrapper"] > div { padding: .7rem .85rem; }

        /* 헤더 */
        .vpp-header { display: flex; align-items: center; gap: .5rem; margin-bottom: .2rem; }
        .vpp-deco {
            flex: 1; height: 1px; position: relative;
            background-image: repeating-linear-gradient(90deg,
                rgba(34,211,238,.55) 0 4px, transparent 4px 13px);
        }
        .vpp-deco::before, .vpp-deco::after {
            content: "◎"; position: absolute; top: 50%; transform: translateY(-50%);
            color: var(--vpp-corner); font-size: 13px; line-height: 1;
            text-shadow: 0 0 9px rgba(34,211,238,.85);
        }
        .vpp-deco::before { left: 14%; }
        .vpp-deco::after  { right: 14%; }
        .vpp-title {
            font-size: 1.95rem; font-weight: 800; letter-spacing: .05em; white-space: nowrap;
            padding: 0 1.1rem; color: #4fe3f7;
            text-shadow: 0 0 18px rgba(34,211,238,.55), 0 0 42px rgba(34,211,238,.28);
        }
        .vpp-subtitle {
            text-align: center; color: #7fa8c4; font-size: .78rem;
            letter-spacing: .04em; margin-bottom: 1.1rem;
        }

        /* 알람 배너 */
        .vpp-alarm {
            padding: .62rem 1rem; border-radius: 3px; font-size: .92rem;
            margin: .1rem 0 1.15rem; border-left: 3px solid; letter-spacing: .01em;
        }
        .vpp-alarm.ok { background: rgba(74,222,128,.07); border-color: #4ade80; color: #86efac; }
        .vpp-alarm.warn { background: rgba(251,191,36,.08); border-color: #fbbf24; color: #fcd34d; }
        .vpp-alarm.critical {
            background: rgba(244,63,94,.10); border-color: #fb7185; color: #fda4af;
            animation: vpp-blink 1.6s ease-in-out infinite;
        }
        @keyframes vpp-blink { 0%,100% { opacity: 1; } 50% { opacity: .62; } }

        /* 뱃지 */
        .vpp-badge {
            display: inline-flex; align-items: center; gap: .35rem;
            padding: .18rem .6rem; border-radius: 3px; margin: .12rem .18rem .12rem 0;
            font-size: .74rem; font-weight: 600; line-height: 1.6; border: 1px solid;
        }
        .vpp-badge.ok {
            background: rgba(74,222,128,.09); color: #4ade80;
            border-color: rgba(74,222,128,.35);
        }
        .vpp-badge.warn {
            background: rgba(251,191,36,.09); color: #fbbf24;
            border-color: rgba(251,191,36,.35);
        }
        .vpp-badge.critical {
            background: rgba(244,63,94,.11); color: #fb7185; border-color: rgba(244,63,94,.4);
            box-shadow: 0 0 12px rgba(244,63,94,.22);
        }

        /* 발전원 카드 */
        .vpp-gen-card {
            border: 1px solid; border-left-width: 3px; border-radius: 3px;
            padding: .6rem .7rem .55rem;
        }
        .vpp-gen-head { display: flex; align-items: center; gap: .42rem; margin-bottom: .38rem; }
        .vpp-gen-name { font-size: .82rem; font-weight: 700; letter-spacing: .02em; }
        .vpp-gen-value { font-size: 1.22rem; font-weight: 700; color: #e8f6ff; line-height: 1.1; }
        .vpp-gen-unit { font-size: .66rem; color: #7fa8c4; margin-left: .22rem; font-weight: 600; }
        .vpp-gen-bar {
            height: 4px; border-radius: 2px; background: rgba(255,255,255,.07);
            margin: .45rem 0 .34rem; overflow: hidden;
        }
        .vpp-gen-bar > span { display: block; height: 100%; border-radius: 2px; }
        .vpp-gen-foot {
            display: flex; justify-content: space-between;
            font-size: .67rem; color: #7fa8c4;
        }

        /* 게이지 라벨 (plotly 밖이라 폭 변화에도 안 잘림) */
        .vpp-gauge-label {
            text-align: center; font-size: .76rem; color: #8fb3cc;
            font-weight: 600; letter-spacing: .03em;
            margin: .1rem 0 -.5rem; line-height: 1.35;
        }

        /* 패널 제목 */
        .vpp-section-title {
            font-size: .84rem; font-weight: 600; color: #cfe8f7;
            letter-spacing: .02em; margin: .1rem 0 .5rem;
        }
        .vpp-mock-tag { color: #6b8ba5; font-size: .73rem; }

        /* 메트릭 */
        div[data-testid="stMetricLabel"] { font-size: .76rem; color: #8fb3cc; font-weight: 600; }
        div[data-testid="stMetricValue"] {
            font-size: 1.5rem; font-weight: 700; color: #e8f6ff;
            text-shadow: 0 0 14px rgba(34,211,238,.32);
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
        font=dict(color=C_TEXT, size=11),
        margin=dict(l=12, r=12, t=28, b=12),
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
        hoverlabel=dict(bgcolor="#0b2038", bordercolor="rgba(34,211,238,.45)", font_color=C_BRIGHT),
    )
    axis = dict(
        gridcolor="rgba(34,211,238,.08)",
        zerolinecolor="rgba(34,211,238,.16)",
        linecolor="rgba(34,211,238,.20)",
    )
    fig.update_xaxes(**axis)
    fig.update_yaxes(**axis)
    return fig


def reserve_status(reserve_pct: float) -> tuple[str, str]:
    """예비율 배지 (라벨, css클래스)."""
    if reserve_pct < RESERVE_CRITICAL_PCT:
        return "Critical", "critical"
    if reserve_pct < RESERVE_WARNING_PCT:
        return "Warning", "warn"
    return "Normal", "ok"


def badge(label: str, css_class: str) -> str:
    return f'<span class="vpp-badge {css_class}">{label}</span>'


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def gen_icon(name: str, size: int = 24) -> str:
    """발전원 아이콘 SVG를 해당 발전원 색상 + 네온 글로우로 렌더한다."""
    color = GENERATOR_COLOR[name]
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" '
        f'style="filter:drop-shadow(0 0 5px {color}80);flex:none">'
        f"{_ICON_SVG[name].format(c=color)}</svg>"
    )


def panel_title(text: str) -> None:
    st.markdown(f'<div class="vpp-section-title">{text}</div>', unsafe_allow_html=True)


def gauge_label(text: str) -> None:
    """게이지 위 라벨. plotly 밖(HTML)이라 차트 폭이 변해도 잘리지 않는다."""
    st.markdown(f'<div class="vpp-gauge-label">{text}</div>', unsafe_allow_html=True)


# ── 화면 섹션 ────────────────────────────────────────────────────────────
def render_header() -> None:
    st.markdown(
        '<div class="vpp-header">'
        '<div class="vpp-deco"></div>'
        '<div class="vpp-title">VPP 통합 관제 대시보드</div>'
        '<div class="vpp-deco"></div>'
        "</div>"
        '<div class="vpp-subtitle">'
        "실시간 수급 현황 · MERIT ORDER · 최적화 결과 · 탄소 배출량 · 위기 시나리오<br>"
        '<span class="vpp-mock-tag">'
        "⚠ 더미 데이터 기반 목업 (forecast_api / dispatch_api 미연동)"
        "</span></div>",
        unsafe_allow_html=True,
    )


def render_status_banner(reserve_pct: float) -> None:
    label, css_class = reserve_status(reserve_pct)
    detail = {
        "critical": f"예비율 {reserve_pct:.1f}% — 기준 5% 미만, 즉시 조치 필요",
        "warn": f"예비율 {reserve_pct:.1f}% — 기준 7% 미만, 주의 감시",
        "ok": f"예비율 {reserve_pct:.1f}% — 정상 범위",
    }[css_class]
    icon = {"critical": "■", "warn": "▲", "ok": "●"}[css_class]
    st.markdown(
        f'<div class="vpp-alarm {css_class}">{icon} <b>{label}</b> &nbsp;|&nbsp; {detail}</div>',
        unsafe_allow_html=True,
    )


def _gauge_figure(
    value: float, value_max: float, suffix: str = "",
    steps: list[dict] | None = None, value_min: float = 0.0,
) -> go.Figure:
    """게이지 도형만 그린다.

    라벨을 plotly title로 넣으면 패널 폭이 넓어질 때(사이드바 접힘 등) 아크가
    커지면서 상단 여백 밖으로 밀려 잘린다. 라벨은 gauge_label()로 따로 붙인다.
    """
    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=value,
            number={"suffix": suffix, "font": {"color": C_BRIGHT, "size": 26}},
            gauge={
                "axis": {
                    "range": [value_min, value_max],
                    "tickcolor": "#5b7f9b",
                    "tickfont": {"size": 9, "color": "#7fa8c4"},
                },
                "bar": {"color": C_CYAN, "thickness": 0.72},
                "bgcolor": "rgba(255,255,255,.03)",
                "borderwidth": 0,
                "steps": steps or [],
            },
        )
    )
    fig = style_dark(fig, height=215)
    fig.update_layout(margin=dict(l=18, r=18, t=14, b=8))
    return fig


def render_supply_gauges(row: pd.Series) -> None:
    total_supply = (
        row.nuclear_mw + row.hydro_mw + row.solar_mw + row.wind_mw
        + row.coal_mw + row.lng_mw + row.ess_discharge_mw
    )
    reserve_steps = [
        {"range": [0, RESERVE_CRITICAL_PCT], "color": "rgba(244,63,94,.28)"},
        {"range": [RESERVE_CRITICAL_PCT, RESERVE_WARNING_PCT], "color": "rgba(251,191,36,.25)"},
        {"range": [RESERVE_WARNING_PCT, 30], "color": "rgba(74,222,128,.18)"},
    ]
    c1, c2, c3 = st.columns(3)
    with c1, st.container(border=True):
        panel_title("수요 (MW)")
        st.plotly_chart(_gauge_figure(row.demand_mw, 100000), width="stretch", key="g_demand")
    with c2, st.container(border=True):
        panel_title("공급 (MW)")
        st.plotly_chart(_gauge_figure(total_supply, 100000), width="stretch", key="g_supply")
    with c3, st.container(border=True):
        panel_title("예비율 (%)")
        st.plotly_chart(
            _gauge_figure(row.reserve_pct, 30, suffix="%", steps=reserve_steps),
            width="stretch", key="g_reserve",
        )


def render_generator_cards(row: pd.Series) -> None:
    """발전원 6종을 아이콘 카드로 분리해 현재 출력·이용률·탄소량을 보여준다."""
    panel_title("발전원별 현황")
    outputs = {
        "원자력": row.nuclear_mw, "수력": row.hydro_mw, "태양광": row.solar_mw,
        "풍력": row.wind_mw, "석탄": row.coal_mw, "LNG": row.lng_mw,
    }
    for col, name in zip(st.columns(6), GENERATOR_ORDER):
        mw = outputs[name]
        color = GENERATOR_COLOR[name]
        r, g, b = _hex_to_rgb(color)
        usage = min(mw / CAPACITY_MW[name] * 100, 100.0)
        co2 = mw * CO2_TON_PER_MWH[name]
        col.markdown(
            f'<div class="vpp-gen-card" style="'
            f"background:linear-gradient(180deg,rgba({r},{g},{b},.15),rgba({r},{g},{b},.03));"
            f"border-color:rgba({r},{g},{b},.30);border-left-color:{color}\">"
            f'<div class="vpp-gen-head">{gen_icon(name)}'
            f'<span class="vpp-gen-name" style="color:{color}">{name}</span></div>'
            f'<div class="vpp-gen-value">{mw:,.0f}<span class="vpp-gen-unit">MW</span></div>'
            f'<div class="vpp-gen-bar">'
            f'<span style="width:{usage:.1f}%;background:{color}"></span></div>'
            f'<div class="vpp-gen-foot"><span>이용률 {usage:.0f}%</span>'
            f"<span>{co2:,.0f} tCO2</span></div></div>",
            unsafe_allow_html=True,
        )


def render_merit_order(df: pd.DataFrame) -> None:
    panel_title("Merit Order 스택 차트 (시간대별 급전 구성)")
    label_map = {
        "nuclear_mw": "원자력", "hydro_mw": "수력", "solar_mw": "태양광",
        "wind_mw": "풍력", "coal_mw": "석탄", "lng_mw": "LNG",
    }
    melted = df.melt(
        id_vars=["hour"], value_vars=list(label_map),
        var_name="generator", value_name="mw",
    )
    melted["generator"] = melted["generator"].map(label_map)

    fig = px.bar(
        melted, x="hour", y="mw", color="generator",
        category_orders={"generator": GENERATOR_ORDER},
        color_discrete_map=GENERATOR_COLOR,
        labels={"hour": "시간", "mw": "출력 (MW)", "generator": "발전원"},
    )
    fig.add_scatter(
        x=df["hour"], y=df["demand_mw"], mode="lines", name="수요",
        line=dict(color=C_YELLOW, width=2, dash="dot"),
    )
    fig.update_traces(marker_line_width=0, selector=dict(type="bar"))
    fig.update_layout(barmode="stack", legend=dict(orientation="h", y=-0.18))
    st.plotly_chart(style_dark(fig, height=360), width="stretch", key="c_merit")


def render_dispatch_summary(df: pd.DataFrame) -> None:
    panel_title("최적화 결과 (MILP)")
    mock_saving_pct = 12.4
    r1c1, r1c2 = st.columns(2)
    r1c1.metric("총 비용 (일간)", "43.1억원")
    r1c2.metric("절감률", f"{mock_saving_pct:.1f}%", delta="목표 10%↑")
    r2c1, r2c2 = st.columns(2)
    r2c1.metric("API 응답", "3.2초", delta="목표 10초↓")
    r2c2.metric("시나리오", "12개", delta="목표 10개↑")

    constraints = [
        "수급균형", "예비율≥10%", "출력 상하한",
        "램프율", "최소기동정지", "수력저수율<30%",
    ]
    mock_pass = [True, True, True, True, True, True]
    st.markdown(
        "".join(
            badge(f"{i + 1}. {name} ✓" if ok else f"{i + 1}. {name} ✗", "ok" if ok else "critical")
            for i, (name, ok) in enumerate(zip(constraints, mock_pass))
        ),
        unsafe_allow_html=True,
    )


def render_carbon(df: pd.DataFrame) -> None:
    panel_title("탄소 배출량 추이")
    fig = px.area(df, x="hour", y="carbon_ton", labels={"hour": "시간", "carbon_ton": "tCO2"})
    fig.update_traces(line_color=C_PINK, fillcolor="rgba(244,114,182,.18)")
    st.plotly_chart(style_dark(fig, height=290), width="stretch", key="c_carbon")
    st.metric("일간 누적 배출량", f"{df['carbon_ton'].sum():,.0f} tCO2")


def render_ess(df: pd.DataFrame) -> None:
    panel_title("ESS 충/방전 · SoC")
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_bar(x=df["hour"], y=df["ess_discharge_mw"], name="방전", marker_color=C_VIOLET)
    fig.add_bar(x=df["hour"], y=-df["ess_charge_mw"], name="충전", marker_color="#4f46e5")
    fig.add_scatter(
        x=df["hour"], y=df["ess_soc_pct"], name="SoC (%)",
        line=dict(color=C_YELLOW, width=2), secondary_y=True,
    )
    fig.update_traces(marker_line_width=0, selector=dict(type="bar"))
    fig.update_layout(barmode="relative", legend=dict(orientation="h", y=-0.18))
    fig.update_yaxes(title_text="MW", secondary_y=False)
    fig.update_yaxes(
        title_text="SoC (%)", range=[0, 100], secondary_y=True,
        gridcolor="rgba(0,0,0,0)", linecolor="rgba(34,211,238,.20)",
    )
    st.plotly_chart(style_dark(fig, height=290), width="stretch", key="c_ess")

    peak_before = df["demand_mw"].max()
    peak_after = (df["demand_mw"] - df["ess_discharge_mw"] + df["ess_charge_mw"]).max()
    peak_cut_pct = (peak_before - peak_after) / peak_before * 100
    st.metric("피크 감소율", f"{peak_cut_pct:.1f}%", delta="목표 5%↑")


def render_crisis_simulator(df: pd.DataFrame, hour: int) -> None:
    panel_title("위기 시나리오 시뮬레이터")
    scenario = st.selectbox("시나리오", list(CRISIS_SCENARIOS), key="crisis_scenario")

    row = df.loc[df.hour == hour].iloc[0]
    new_demand, new_capacity = apply_scenario(row, scenario)
    new_reserve = (new_capacity - new_demand) / new_demand * 100

    steps = [
        {"range": [-30, RESERVE_CRITICAL_PCT], "color": "rgba(244,63,94,.28)"},
        {"range": [RESERVE_CRITICAL_PCT, RESERVE_WARNING_PCT], "color": "rgba(251,191,36,.25)"},
        {"range": [RESERVE_WARNING_PCT, 30], "color": "rgba(74,222,128,.18)"},
    ]
    # 두 게이지는 눈금 범위를 반드시 동일하게 둔다 — 다르면 같은 값도 바늘 위치가
    # 달라 보여 before/after 비교가 왜곡된다.
    c1, c2 = st.columns(2)
    with c1:
        gauge_label(f"{hour}시 평시 (%)")
        st.plotly_chart(
            _gauge_figure(row.reserve_pct, 30, suffix="%", steps=steps, value_min=-30),
            width="stretch", key="g_crisis_base",
        )
    with c2:
        gauge_label(f"{hour}시 · {scenario} (%)")
        st.plotly_chart(
            _gauge_figure(new_reserve, 30, suffix="%", steps=steps, value_min=-30),
            width="stretch", key="g_crisis_after",
        )

    label, css_class = reserve_status(new_reserve)
    ok = new_reserve >= RESERVE_CRITICAL_PCT
    verdict = "유지 ✓" if ok else "위반 ✗"
    st.markdown(
        badge(f"{label} — 예비율 5% 이상 {verdict}", "ok" if ok else "critical"),
        unsafe_allow_html=True,
    )


def render_stochastic_panel() -> None:
    panel_title("확률론적 최적화 (선택 A · Two-Stage)")
    st.metric("VSS", "4.8%", delta="목표 3%↑")
    c1, c2 = st.columns(2)
    c1.metric("시나리오 수", "10개")
    c2.metric("Recourse 비용", "2.1억원")
    st.caption("VSS = (평균값 문제 기대비용) − (2단계 확률계획 비용). Pyomo 결과 연동 예정.")


def render_generation_heatmap(df: pd.DataFrame) -> None:
    panel_title("발전원별 시간대 출력 히트맵")
    label_map = {
        "nuclear_mw": "원자력", "hydro_mw": "수력", "solar_mw": "태양광",
        "wind_mw": "풍력", "coal_mw": "석탄", "lng_mw": "LNG",
    }
    melted = df.melt(id_vars=["hour"], value_vars=list(label_map), var_name="g", value_name="mw")
    melted["g"] = melted["g"].map(label_map)
    pivot = melted.pivot(index="g", columns="hour", values="mw").reindex(GENERATOR_ORDER[::-1])

    fig = px.imshow(
        pivot, aspect="auto", color_continuous_scale=HEAT_SCALE,
        labels={"x": "시간", "y": "발전원", "color": "MW"},
    )
    fig.update_coloraxes(colorbar=dict(outlinewidth=0, tickfont=dict(color=C_TEXT, size=9)))
    st.plotly_chart(style_dark(fig, height=260), width="stretch", key="c_heatmap")


def main() -> None:
    st.set_page_config(page_title="VPP 통합 관제 대시보드", page_icon="⚡", layout="wide")
    inject_custom_css()

    df = mock_timeseries()

    with st.sidebar:
        st.markdown('<div class="vpp-section-title">관측 설정</div>', unsafe_allow_html=True)
        hour = st.slider("관측 시각 (시)", 0, 23, 19)
        st.markdown(
            badge("DATA SOURCE : MOCK", "warn")
            + '<div class="vpp-mock-tag">forecast_api / dispatch_api 미연동</div>',
            unsafe_allow_html=True,
        )

    now = df.loc[df.hour == hour].iloc[0]

    render_header()
    render_status_banner(now.reserve_pct)
    render_supply_gauges(now)

    with st.container(border=True):
        render_generator_cards(now)

    left, right = st.columns([2, 1])
    with left, st.container(border=True):
        render_merit_order(df)
    with right, st.container(border=True):
        render_dispatch_summary(df)

    c1, c2 = st.columns(2)
    with c1, st.container(border=True):
        render_carbon(df)
    with c2, st.container(border=True):
        render_ess(df)

    c3, c4 = st.columns([2, 1])
    with c3, st.container(border=True):
        render_crisis_simulator(df, hour)
    with c4, st.container(border=True):
        render_stochastic_panel()

    with st.container(border=True):
        render_generation_heatmap(df)


if __name__ == "__main__":
    main()
