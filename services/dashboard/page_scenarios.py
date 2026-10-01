"""시나리오 생성기 페이지 — dispatch_api POST /api/v1/scenarios 결과를 확인한다.

생성 조건을 바꿔 다시 뽑아 보고, 시나리오별 경로·확률·라벨과 검증 결과를 본다.
여기서 만든 시나리오 묶음이 Two-Stage 최적화의 입력이 된다 (formulation.md 7~8절).
"""

from __future__ import annotations

import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import logic
from logic import API_PREFIX, ApiResult
from ui import (
    C_GREEN,
    C_TEXT,
    C_YELLOW,
    badge,
    get_settings,
    panel_title,
    pw,
    source_tag,
    style_dark,
)

# 생성 조건 기본값 (shared/schemas.py ScenarioRequest 와 같다)
DEFAULTS: dict[str, float] = {
    "n_scenarios": 10,
    "n_samples": 300,
    "seed": 42,
    "demand_sigma_pct": 2.5,
    "demand_ar": 0.9,
    "solar_cloud_pct": 10.0,
    "wind_sigma_pct": 25.0,
}

VARIABLES = {
    "순부하": ("net", "수요 − 태양광 − 풍력. 발전기가 실제로 메워야 하는 양"),
    "수요": ("demand_mw", "전력 수요"),
    "태양광": ("solar_mw", "태양광 출력. 흐린 날 하방으로 크게 빠진다"),
    "풍력": ("wind_mw", "풍력 출력"),
}


@st.cache_data(ttl=600, show_spinner="시나리오 생성 중…")
def fetch_scenarios(base_url: str, payload_json: str) -> ApiResult:
    """POST /scenarios. 같은 조건이면 캐시를 쓴다 (payload 를 JSON 문자열로 받아 키로 쓴다)."""
    return logic.call_api(
        "POST", f"{base_url}{API_PREFIX}/scenarios", payload=json.loads(payload_json), timeout=30
    )


def scenario_frame(data: dict) -> pd.DataFrame:
    """시나리오 요약표 (한 행 = 시나리오 하나)."""
    return pd.DataFrame([
        {
            "ID": sc["scenario_id"],
            "라벨": sc["label"],
            "확률": sc["probability"],
            "피크 순부하(MW)": sc["peak_net_load_mw"],
            "수요 편차(%)": sc["demand_dev_pct"],
            "태양광 편차(%)": sc["solar_dev_pct"],
            "풍력 편차(%)": sc["wind_dev_pct"],
        }
        for sc in data["scenarios"]
    ])


def series(sc: dict, key: str) -> list[float]:
    """시나리오(또는 기준 예측)의 시간별 값. key='net' 이면 순부하를 계산한다."""
    if key == "net":
        return [d - s - w for d, s, w in zip(sc["demand_mw"], sc["solar_mw"], sc["wind_mw"])]
    return sc[key]


def long_table(data: dict) -> pd.DataFrame:
    """최적화 입력용 긴 표 (시나리오 x 시간). CSV 내려받기에 쓴다."""
    rows = []
    for sc in data["scenarios"]:
        for h, (d, s, w) in enumerate(zip(sc["demand_mw"], sc["solar_mw"], sc["wind_mw"])):
            rows.append({
                "scenario_id": sc["scenario_id"], "probability": sc["probability"], "hour": h,
                "demand_mw": d, "solar_mw": s, "wind_mw": w, "net_load_mw": d - s - w,
            })
    return pd.DataFrame(rows)


# ── 화면 조각 ────────────────────────────────────────────────────────────────


def render_controls() -> dict:
    """사이드바 생성 조건 폼. '시나리오 생성'을 눌러야 반영된다. 적용된 조건을 돌려준다."""
    st.session_state.setdefault("scn_params", dict(DEFAULTS))
    current = st.session_state.scn_params

    with st.sidebar:
        panel_title("생성 조건")
        with st.form("scenario_form", border=False):
            n_scenarios = st.slider(
                "시나리오 수", 10, 30, int(current["n_scenarios"]),
                help="요구사항: 10개 이상. 상·하방 꼬리 1개씩 + 나머지 군집 대표",
            )
            n_samples = st.select_slider(
                "축약 전 표본 수", [100, 200, 300, 500, 1000], value=int(current["n_samples"])
            )
            seed = st.number_input("seed (같으면 같은 결과)", 0, 9999, int(current["seed"]))
            st.markdown("**오차 크기**")
            demand_sigma = st.slider(
                "수요 오차 σ (%)", 0.5, 6.0, float(current["demand_sigma_pct"]), 0.5,
                help="24h ahead MAPE 4% 기준이면 σ 2~3%가 적당",
            )
            demand_ar = st.slider(
                "수요 오차 시간상관", 0.0, 0.99, float(current["demand_ar"]), 0.01,
                help="클수록 하루 내내 같은 방향으로 빗나간다",
            )
            solar_cloud = st.slider(
                "태양광 흐림 강도 (%)", 0.0, 40.0, float(current["solar_cloud_pct"]), 2.5,
                help="하루 공통 흐림 계수의 평균 감소폭. 클수록 흐린 날이 자주·깊게",
            )
            wind_sigma = st.slider(
                "풍력 오차 σ (%)", 0.0, 60.0, float(current["wind_sigma_pct"]), 5.0
            )
            submitted = st.form_submit_button("시나리오 생성", type="primary", width="stretch")

        if submitted:
            st.session_state.scn_params = {
                "n_scenarios": n_scenarios, "n_samples": n_samples, "seed": int(seed),
                "demand_sigma_pct": demand_sigma, "demand_ar": demand_ar,
                "solar_cloud_pct": solar_cloud, "wind_sigma_pct": wind_sigma,
            }
        if st.session_state.scn_params != DEFAULTS and st.button("기본 조건으로", width="stretch"):
            st.session_state.scn_params = dict(DEFAULTS)
            st.rerun()
    return st.session_state.scn_params


def render_summary(data: dict, res: ApiResult) -> None:
    """핵심 숫자와 검증 배지."""
    frame = scenario_frame(data)
    base_peak = max(series(data["base"], "net"))
    expected_peak = float((frame["확률"] * frame["피크 순부하(MW)"]).sum())

    panel_title("생성 결과", source_tag(True, res.latency_s))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("시나리오", f"{len(frame)}개", f"표본 {data['n_samples']}개에서 축약",
              delta_color="off")
    c2.metric("기준 예측 피크 순부하", pw(base_peak))
    c3.metric("확률가중 피크 순부하", pw(expected_peak),
              f"{(expected_peak - base_peak) / base_peak * 100:+.1f}%", delta_color="off")
    c4.metric("최대 피크 순부하", pw(frame["피크 순부하(MW)"].max()),
              f"{frame.iloc[0]['ID']} · {frame.iloc[0]['라벨']}", delta_color="off")

    st.markdown(
        "".join(
            badge(f"{c['name']} {'✓' if c['ok'] else '✗'}", "ok" if c["ok"] else "critical")
            for c in data["checks"]
        ),
        unsafe_allow_html=True,
    )
    with st.expander("검증 상세"):
        names = {"name": "항목", "ok": "통과", "detail": "내용"}
        st.dataframe(
            pd.DataFrame(data["checks"]).rename(columns=names), hide_index=True, width="stretch"
        )


def render_fan_chart(data: dict, selected: str) -> None:
    """시나리오 경로 묶음. 선 굵기 = 확률, 선택한 시나리오는 노랑으로 강조."""
    panel_title("시나리오 경로 (선 굵기 = 확률)")
    name = st.radio("변수", list(VARIABLES), horizontal=True, key="scn_var",
                    label_visibility="collapsed")
    key, desc = VARIABLES[name]
    hours = list(range(data["horizon_h"]))

    fig = go.Figure()
    band = {"net": data["net_load_band"], "demand_mw": data["demand_band"]}.get(key)
    if band:
        fig.add_scatter(x=hours, y=band["p95"], mode="lines", line=dict(width=0),
                        showlegend=False, hoverinfo="skip")
        fig.add_scatter(x=hours, y=band["p5"], mode="lines", line=dict(width=0), fill="tonexty",
                        fillcolor="rgba(163,230,53,.10)", name="표본 P5–P95")

    for sc in data["scenarios"]:
        is_sel = sc["scenario_id"] == selected
        fig.add_scatter(
            x=hours, y=series(sc, key), mode="lines",
            name=f"{sc['scenario_id']} {sc['label']}",
            line=dict(
                color=C_YELLOW if is_sel else "rgba(163,230,53,.45)",
                width=(3.5 if is_sel else 1) + sc["probability"] * 12,
            ),
            opacity=1 if is_sel else 0.8,
            hovertemplate=(f"{sc['scenario_id']} · {sc['label']} · p={sc['probability']:.1%}"
                           "<br>%{x}시 %{y:,.0f} MW<extra></extra>"),
            showlegend=is_sel,
        )
    fig.add_scatter(x=hours, y=series(data["base"], key), mode="lines", name="기준 예측",
                    line=dict(color="#f4f5f6", width=2, dash="dash"))
    fig.update_layout(legend=dict(orientation="h", y=-0.18), xaxis_title="시간", yaxis_title="MW")
    st.plotly_chart(style_dark(fig, height=380), width="stretch", key="c_fan")
    st.caption(desc)


def render_table(data: dict) -> str:
    """시나리오 요약표. 행을 고르면 그 시나리오를 강조·상세 표시한다. 선택 ID 를 돌려준다."""
    panel_title("시나리오 목록 (행을 클릭해 선택)")
    frame = scenario_frame(data)
    event = st.dataframe(
        frame, hide_index=True, width="stretch", key="scn_table",
        on_select="rerun", selection_mode="single-row",
        column_config={
            "확률": st.column_config.ProgressColumn("확률", format="percent", min_value=0,
                                                   max_value=float(frame["확률"].max())),
            "피크 순부하(MW)": st.column_config.NumberColumn(format="localized"),
            "수요 편차(%)": st.column_config.NumberColumn(format="%+.1f"),
            "태양광 편차(%)": st.column_config.NumberColumn(format="%+.1f"),
            "풍력 편차(%)": st.column_config.NumberColumn(format="%+.1f"),
        },
    )
    rows = event.selection.rows if event and event.selection else []
    st.caption("편차 = 기준 예측 대비 일간 에너지 차이. 라벨 기준: 수요 ±1.5%, "
               "태양광 −15% 흐림 / +5% 맑음, 풍력 ±20%.")
    return frame.iloc[rows[0]]["ID"] if rows else frame.iloc[0]["ID"]


def render_detail(data: dict, selected: str) -> None:
    """선택한 시나리오의 수요·태양광·풍력을 기준 예측과 겹쳐 본다."""
    sc = next(s for s in data["scenarios"] if s["scenario_id"] == selected)
    panel_title(f"{sc['scenario_id']} 상세 — {sc['label']} (확률 {sc['probability']:.1%})")
    hours = list(range(data["horizon_h"]))
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06,
                        subplot_titles=("수요", "태양광", "풍력"))
    for row, key in enumerate(("demand_mw", "solar_mw", "wind_mw"), start=1):
        fig.add_scatter(x=hours, y=data["base"][key], mode="lines", name="기준 예측",
                        line=dict(color=C_TEXT, width=1.3, dash="dash"), showlegend=row == 1,
                        row=row, col=1)
        fig.add_scatter(x=hours, y=sc[key], mode="lines", name=sc["scenario_id"],
                        line=dict(color=C_YELLOW, width=2), showlegend=row == 1, row=row, col=1)
    fig.update_annotations(font=dict(size=11, color=C_TEXT))
    fig.update_layout(legend=dict(orientation="h", y=-0.08))
    st.plotly_chart(style_dark(fig, height=430), width="stretch", key="c_detail")


def render_probability(data: dict) -> None:
    """시나리오별 확률 막대."""
    panel_title("확률 분포")
    frame = scenario_frame(data).iloc[::-1]
    fig = go.Figure(go.Bar(
        x=frame["확률"], y=frame["ID"] + " " + frame["라벨"], orientation="h",
        marker_color=C_GREEN, marker_line_width=0, marker_cornerradius=6,
        text=[f"{p:.1%}" for p in frame["확률"]], textposition="outside",
    ))
    fig.update_layout(xaxis_tickformat=".0%", margin=dict(l=12, r=40, t=10, b=12))
    st.plotly_chart(style_dark(fig, height=430), width="stretch", key="c_prob")


def render_export(data: dict, params: dict) -> None:
    """최적화 입력으로 넘길 형태로 내려받기."""
    panel_title("최적화 입력으로 내보내기")
    stochastic_input = {
        "scenarios": [
            {k: sc[k] for k in ("scenario_id", "probability", "demand_mw", "solar_mw", "wind_mw")}
            for sc in data["scenarios"]
        ],
        "generator_params": params,
    }
    c1, c2 = st.columns(2)
    c1.download_button(
        "JSON (/dispatch/stochastic 의 scenarios)",
        json.dumps(stochastic_input, ensure_ascii=False, indent=1).encode("utf-8"),
        file_name="scenarios.json", mime="application/json", width="stretch",
    )
    c2.download_button(
        "CSV (시나리오 x 시간)", long_table(data).to_csv(index=False).encode("utf-8-sig"),
        file_name="scenarios.csv", mime="text/csv", width="stretch",
    )


# ── 페이지 ───────────────────────────────────────────────────────────────────


def render() -> None:
    """시나리오 생성기 페이지."""
    get_settings()  # 설정 파일이 없으면 여기서 바로 드러나게
    params = render_controls()
    with st.sidebar:
        st.toggle("큰 단위로 보기 (GW · 억원 · 천t)", value=True, key="compact_units")

    st.markdown(
        '<div class="vpp-topbar">'
        '<div class="vpp-title"><span class="vpp-logo">⚡</span>불확실성 시나리오 생성기</div>'
        '<div class="vpp-subtitle">예측 오차 표본 → 꼬리 보존 축약 → 확률 시나리오 '
        "(Two-Stage 최적화 입력)</div></div>",
        unsafe_allow_html=True,
    )

    url = logic.service_urls()["dispatch_api"]
    res = fetch_scenarios(url, json.dumps(params, sort_keys=True))
    if not res.ok:
        fetch_scenarios.clear()  # 실패는 캐시하지 않는다 — 서비스를 띄우면 바로 다시 시도되게
        st.error(
            f"dispatch_api 에서 시나리오를 받지 못했다 ({res.error}).\n\n"
            "`docker compose up -d --build dispatch_api` 로 띄운 뒤 '시나리오 생성'을 다시 누른다. "
            f"(요청 주소: {url}{API_PREFIX}/scenarios)"
        )
        return
    data = res.data

    with st.container(border=True):
        render_summary(data, res)

    with st.container(border=True):
        selected = st.session_state.get("scn_selected", data["scenarios"][0]["scenario_id"])
        if selected not in {s["scenario_id"] for s in data["scenarios"]}:
            selected = data["scenarios"][0]["scenario_id"]
        render_fan_chart(data, selected)

    left, right = st.columns([3, 2])
    with left, st.container(border=True):
        chosen = render_table(data)
        if chosen != selected:
            st.session_state.scn_selected = chosen
            st.rerun()
        render_detail(data, selected)
    with right, st.container(border=True):
        render_probability(data)

    with st.container(border=True):
        render_export(data, params)
