"""Two-Stage 성능 비교 (테스트) 페이지 — formulation.md 3장(Two-Stage · VSS)을 실제로 돌려 본다.

POST /api/v1/scenarios 로 시나리오를 만들고, 그 묶음을 그대로
POST /api/v1/dispatch/stochastic 에 넘겨 RP · EV · EEV · WS 를 비교한다.
VSS 는 RP 가 수렴했고 솔버 허용오차보다 클 때만 믿을 수 있어서, 그 두 가지를 같이 판정한다.
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import logic
from logic import API_PREFIX, ApiResult
from page_scenarios import fetch_scenarios
from ui import (
    C_BLUE,
    C_BRIGHT,
    C_GREEN,
    C_ORANGE,
    C_RED,
    C_TEXT,
    CFG,
    HEAT_SCALE,
    badge,
    get_settings,
    kpi_badge,
    panel_title,
    source_tag,
    style_dark,
    won,
)

# 실행 조건 초기값. config/dashboard.yaml 의 page_defaults.stochastic_test.
# 제한 시간은 API 기본(10초)보다 길게 둔다 — 25기 x 24h x 10 시나리오의
# RP 는 HiGHS 버전·PC 에 따라 10초를 넘기고, 덜 풀리면 VSS 가 음수로 나온다.
DEFAULTS: dict[str, float | bool] = dict(CFG.page_defaults["stochastic_test"])
# 한 번 호출에 푸는 문제 수: RP·EV·EEV (정의상 3, WS 를 켜면 시나리오 수만큼 더)
SOLVES_PER_CALL = 3

SCENARIO_KEYS = ("scenario_id", "probability", "demand_mw", "solar_mw", "wind_mw")

PROBLEMS = {
    "ws_won": ("WS", "시나리오를 미리 안다고 본 하한"),
    "rp_won": ("RP", "확률론적 해의 기대비용"),
    "eev_won": ("EEV", "평균 계획을 실제 불확실성 아래 운영한 기대비용"),
    "ev_won": ("EV", "평균 시나리오 1개만 본 계획 비용 (불확실성 없음)"),
}

COST_PARTS = {
    "fuel_won": "연료비",
    "startup_won": "기동비",
    "recourse_won": "예비력 부족 페널티",
    "shed_won": "부하차단 페널티",
}


# ── 계산 (화면 없이 테스트 가능) ─────────────────────────────────────────────


def request_timeout(
    time_limit_s: float, n_scenarios: int, compute_ws: bool, margin_s: float | None = None
) -> float:
    """HTTP 타임아웃. RP·EV·EEV 3번 + (WS 면 시나리오 수만큼) 풀고, 모델 생성 여유를 더한다.

    margin_s 를 안 주면 config 의 api.stochastic_timeout_margin.
    """
    margin_s = CFG.api.stochastic_timeout_margin_s if margin_s is None else margin_s
    solves = SOLVES_PER_CALL + (n_scenarios if compute_ws else 0)
    return time_limit_s * solves + margin_s


def summarize(data: dict, mip_gap: float) -> dict:
    """StochasticResponse 에서 비교에 쓰는 값과 판정 근거를 뽑는다.

    tol_won = mip_gap x EEV 는 솔버가 보장하지 않는 폭이다. WS <= RP <= EEV 는 이 폭 안에서만
    확인하고, VSS 도 이 폭을 넘어야 솔버 오차가 아니라고 본다.
    """
    vss = data["vss"]
    rp, ev, eev = (float(vss[k]) for k in ("rp_won", "ev_won", "eev_won"))
    ws = vss.get("ws_won")
    ws = None if ws is None else float(ws)
    tol = mip_gap * eev
    vss_won = eev - rp
    return {
        "status": data["status"],
        "converged": data["status"] == "optimal",
        "solve_time_s": float(data["solve_time_s"]),
        "rp_won": rp, "ev_won": ev, "eev_won": eev, "ws_won": ws,
        "vss_won": vss_won,
        "vss_pct": vss_won / eev * 100 if eev else 0.0,
        "evpi_won": None if ws is None else rp - ws,
        "tol_won": tol,
        "order_ok": rp <= eev + tol and (ws is None or ws <= rp + tol),
        "significant": vss_won > tol,
    }


def verdict(summary: dict, target_pct: float) -> tuple[str, str]:
    """(판정 문구, 배지 css). 수렴 → 순서 → 유의성 → 목표 순으로 본다."""
    if not summary["converged"]:
        return f"판정 보류 — 미수렴 ({summary['status']})", "warn"
    if not summary["order_ok"]:
        return "판정 보류 — WS ≤ RP ≤ EEV 가 깨졌다", "critical"
    if not summary["significant"]:
        return "VSS 가 솔버 허용오차 이내", "warn"
    if summary["vss_pct"] >= target_pct:
        return f"VSS 목표 달성 (≥ {target_pct:g}%)", "ok"
    return f"VSS 목표 미달 (< {target_pct:g}%)", "critical"


def problem_frame(summary: dict) -> pd.DataFrame:
    """네 문제의 비용표. RP 대비 차이를 같이 둔다."""
    rows = [
        {"문제": name, "비용(원)": summary[key], "RP 대비(원)": summary[key] - summary["rp_won"],
         "의미": desc}
        for key, (name, desc) in PROBLEMS.items()
        if summary[key] is not None
    ]
    return pd.DataFrame(rows)


def breakdown_frame(data: dict) -> pd.DataFrame:
    """RP 기대비용의 구성."""
    cost = data["expected_cost"]
    total = float(cost["total_won"]) or 1.0
    return pd.DataFrame([
        {"항목": label, "비용(원)": float(cost[key]), "비중(%)": float(cost[key]) / total * 100}
        for key, label in COST_PARTS.items()
    ])


def scenario_frame(data: dict, labels: dict[str, str]) -> pd.DataFrame:
    """시나리오별 2단계 결과 (RP 의 1단계 해 기준)."""
    return pd.DataFrame([
        {
            "ID": sc["scenario_id"],
            "라벨": labels.get(sc["scenario_id"], ""),
            "확률": sc["probability"],
            "비용(원)": sc["cost"]["total_won"],
            "연료비(원)": sc["cost"]["fuel_won"],
            "기대 출력 대비 편차(MWh)": sc["rt_adjust_mwh"],
            "부하차단(MWh)": sc["shed_mwh"],
        }
        for sc in data["scenarios"]
    ])


def commitment_matrix(data: dict) -> pd.DataFrame:
    """유닛별 기대 출력 (행 = 유닛, 열 = 시간). 꺼진 칸은 0."""
    frame = pd.DataFrame(data["first_stage"])
    frame["hour"] = frame.groupby("unit_id").cumcount()
    frame["p_mw"] = frame["p_mw"].where(frame["on"], 0.0)
    order = list(dict.fromkeys(frame["unit_id"]))
    return frame.pivot(index="unit_id", columns="hour", values="p_mw").reindex(order)


def history_row(params: dict, summary: dict) -> dict:
    """실행 이력 한 줄 (조건 + 결과)."""
    return {
        "seed": params["seed"],
        "시나리오": params["n_scenarios"],
        "제한(초)": params["time_limit_s"],
        "gap(%)": params["mip_gap_pct"],
        "상태": summary["status"],
        "RP(억원)": summary["rp_won"] / logic.WON_PER_EOK,
        "EEV(억원)": summary["eev_won"] / logic.WON_PER_EOK,
        "VSS(%)": summary["vss_pct"],
        "풀이(초)": summary["solve_time_s"],
    }


# ── 호출 ─────────────────────────────────────────────────────────────────────


@st.cache_data(
    ttl=CFG.api.ttl_stochastic_s,
    show_spinner="Two-Stage 최적화 실행 중… (제한 시간만큼 걸릴 수 있다)",
)
def fetch_stochastic(base_url: str, payload_json: str, timeout: float) -> ApiResult:
    """POST /dispatch/stochastic. 같은 조건이면 캐시를 쓴다."""
    return logic.call_api(
        "POST", f"{base_url}{API_PREFIX}/dispatch/stochastic",
        payload=json.loads(payload_json), timeout=timeout,
    )


def stochastic_payload(params: dict, scenario_set: dict) -> dict:
    """생성한 시나리오 묶음을 그대로 넘기는 StochasticRequest."""
    return {
        "start": f"{date.today().isoformat()}T00:00:00+09:00",
        "horizon_h": scenario_set["horizon_h"],
        "scenarios": [{k: sc[k] for k in SCENARIO_KEYS} for sc in scenario_set["scenarios"]],
        "compute_ws": bool(params["compute_ws"]),
        "solver": {
            "time_limit_s": float(params["time_limit_s"]),
            "mip_gap": float(params["mip_gap_pct"]) / 100,
        },
    }


# ── 화면 조각 ────────────────────────────────────────────────────────────────


def render_controls() -> dict | None:
    """사이드바 실행 조건 폼. '실행'을 누르기 전에는 None 을 돌려준다."""
    current = st.session_state.get("sto_params") or DEFAULTS
    with st.sidebar:
        panel_title("실행 조건")
        with st.form("stochastic_test_form", border=False):
            seed = st.number_input("시나리오 seed", 0, 9999, int(current["seed"]))
            n_scenarios = st.slider("시나리오 수", 10, 20, int(current["n_scenarios"]))
            time_limit = st.select_slider(
                "문제당 제한 시간 (초)", [10, 30, 60, 120], value=int(current["time_limit_s"]),
                help="RP·EV·EEV 각각에 적용된다. API 기본값은 10초",
            )
            gap = st.select_slider(
                "MIP gap (%)", [0.1, 0.5, 1.0], value=float(current["mip_gap_pct"]),
                help="이 폭보다 작은 VSS 는 솔버 오차와 구분되지 않는다",
            )
            ws = st.toggle(
                "WS · EVPI 도 계산", value=bool(current["compute_ws"]),
                help="시나리오 수만큼 더 풀어 느려진다",
            )
            submitted = st.form_submit_button("실행", type="primary", width="stretch")
        if submitted:
            st.session_state.sto_params = {
                "seed": int(seed), "n_scenarios": n_scenarios, "time_limit_s": time_limit,
                "mip_gap_pct": gap, "compute_ws": ws,
            }
    return st.session_state.get("sto_params")


def render_verdict(summary: dict, res: ApiResult) -> None:
    """VSS 와 판정 근거."""
    settings = get_settings()
    label, css = verdict(summary, settings.targets["vss_pct"])
    panel_title("VSS 판정", source_tag(True, res.latency_s))
    st.markdown(badge(label, css), unsafe_allow_html=True)

    cols = st.columns(5)
    cols[0].metric("VSS", f"{summary['vss_pct']:+.2f}%", won(summary["vss_won"]),
                   delta_color="off")
    cols[1].metric("RP (확률론적)", won(summary["rp_won"]))
    cols[2].metric("EEV (결정론적 계획)", won(summary["eev_won"]))
    evpi = summary["evpi_won"]
    cols[3].metric("EVPI", won(evpi) if evpi is not None else "—",
                   None if evpi is not None else "WS 미계산", delta_color="off")
    cols[4].metric("전체 풀이 시간", f"{summary['solve_time_s']:.1f}초")

    def mark(ok: bool) -> str:
        return "ok" if ok else "critical"

    checks = [
        (f"수렴 {'✓' if summary['converged'] else '✗ ' + summary['status']}",
         mark(summary["converged"])),
        (f"WS ≤ RP ≤ EEV {'✓' if summary['order_ok'] else '✗'}", mark(summary["order_ok"])),
        (f"VSS > 허용오차 {won(summary['tol_won'])} {'✓' if summary['significant'] else '✗'}",
         mark(summary["significant"])),
    ]
    st.markdown(
        "".join(badge(text, css_class) for text, css_class in checks)
        + kpi_badge(settings, "vss_pct", "VSS", summary["vss_pct"]),
        unsafe_allow_html=True,
    )
    if not summary["converged"]:
        st.warning(
            "제한 시간 안에 수렴하지 못한 문제가 있다. RP 가 덜 풀리면 EEV 보다 비싸게 나와 "
            "VSS 가 음수가 된다. 제한 시간을 늘려 다시 실행한다."
        )
    elif not summary["significant"]:
        st.info("VSS 가 MIP gap × EEV 보다 작다. 솔버 오차와 구분되지 않는 크기다.")


def render_problems(summary: dict) -> None:
    """RP · EV · EEV · WS 비용 비교. 차이가 작아 RP 대비 차이로 그린다."""
    panel_title("문제별 비용 (RP 대비 차이)")
    frame = problem_frame(summary)
    colors = [C_ORANGE if name == "RP" else C_BLUE for name in frame["문제"]]
    fig = go.Figure(go.Bar(
        x=frame["RP 대비(원)"] / logic.WON_PER_EOK, y=frame["문제"], orientation="h",
        marker_color=colors, marker_line_width=0, marker_cornerradius=6,
        text=[won(v) for v in frame["비용(원)"]], textposition="outside",
        hovertemplate="%{y} · RP 대비 %{x:+,.2f}억원<extra></extra>",
    ))
    fig.add_vline(x=0, line=dict(color=C_TEXT, width=1))
    fig.update_layout(xaxis_title="RP 대비 차이 (억원)", margin=dict(l=12, r=70, t=10, b=12))
    st.plotly_chart(style_dark(fig, height=260), width="stretch", key="c_sto_problems")
    st.dataframe(
        frame, hide_index=True, width="stretch",
        column_config={
            "비용(원)": st.column_config.NumberColumn(format="localized"),
            "RP 대비(원)": st.column_config.NumberColumn(format="localized"),
        },
    )
    st.caption("막대 옆 숫자는 비용 자체다. EV 는 불확실성이 없는 계획 비용이라 EEV·RP 와 "
               "직접 비교하지 않는다.")


def render_breakdown(data: dict) -> None:
    """RP 기대비용 구성."""
    panel_title("RP 기대비용 구성")
    frame = breakdown_frame(data)
    fig = go.Figure(go.Bar(
        x=frame["비용(원)"] / logic.WON_PER_EOK, y=frame["항목"], orientation="h",
        marker_color=C_BLUE, marker_line_width=0, marker_cornerradius=6,
        text=[f"{p:.1f}%" for p in frame["비중(%)"]], textposition="outside",
        hovertemplate="%{y} %{x:,.2f}억원<extra></extra>",
    ))
    fig.update_layout(xaxis_title="억원", yaxis_autorange="reversed",
                      margin=dict(l=12, r=50, t=10, b=12))
    st.plotly_chart(style_dark(fig, height=260), width="stretch", key="c_sto_breakdown")
    shed = float(frame.loc[frame["항목"] == "부하차단 페널티", "비용(원)"].iloc[0])
    st.caption(
        "부하차단 페널티가 0 이 아니면 VSS 가 페널티 값에 좌우된다."
        if shed > 0 else "부하차단이 없다. VSS 는 연료비·기동비 차이에서만 나온다."
    )


def render_scenarios(data: dict, labels: dict[str, str]) -> None:
    """시나리오별 2단계 결과."""
    panel_title("시나리오별 2단계 결과 (RP 의 1단계 해 기준)")
    frame = scenario_frame(data, labels)
    fig = go.Figure(go.Bar(
        x=frame["ID"], y=frame["비용(원)"] / logic.WON_PER_EOK,
        marker_color=[C_RED if s > 0 else C_GREEN for s in frame["부하차단(MWh)"]],
        marker_line_width=0, marker_cornerradius=6, customdata=frame[["라벨", "확률"]],
        hovertemplate="%{x} · %{customdata[0]} · p=%{customdata[1]:.1%}"
                      "<br>비용 %{y:,.2f}억원<extra></extra>",
    ))
    fig.update_layout(xaxis_title="시나리오", yaxis_title="시나리오 비용 (억원)")
    st.plotly_chart(style_dark(fig, height=280), width="stretch", key="c_sto_scenarios")
    st.dataframe(
        frame, hide_index=True, width="stretch",
        column_config={
            "확률": st.column_config.NumberColumn(format="percent"),
            "비용(원)": st.column_config.NumberColumn(format="localized"),
            "연료비(원)": st.column_config.NumberColumn(format="localized"),
            "기대 출력 대비 편차(MWh)": st.column_config.NumberColumn(format="localized"),
            "부하차단(MWh)": st.column_config.NumberColumn(format="localized"),
        },
    )
    st.caption("빨간 막대는 부하차단이 생긴 시나리오다.")


def render_commitment(data: dict) -> None:
    """1단계 기동계획 히트맵. 색은 시나리오 기대 출력."""
    panel_title("1단계 기동계획 (색 = 시나리오 기대 출력)")
    matrix = commitment_matrix(data)
    fig = go.Figure(go.Heatmap(
        z=matrix.values, x=list(matrix.columns), y=list(matrix.index), colorscale=HEAT_SCALE,
        colorbar=dict(title="MW", tickfont=dict(color=C_TEXT)),
        hovertemplate="%{y} · %{x}시 · %{z:,.0f} MW<extra></extra>",
    ))
    fig.update_layout(xaxis_title="시간", yaxis_autorange="reversed",
                      yaxis_tickfont=dict(size=12, color=C_BRIGHT))
    st.plotly_chart(style_dark(fig, height=24 * len(matrix) + 90), width="stretch",
                    key="c_sto_commit")
    st.caption("어두운 칸은 정지. 기동 여부는 모든 시나리오가 공유하고 (비예측성), "
               "출력은 시나리오마다 다르다.")


def render_history() -> None:
    """이 세션에서 돌린 조건별 결과 비교."""
    history = st.session_state.get("sto_history", {})
    panel_title("실행 이력 (이 세션)")
    st.dataframe(
        pd.DataFrame(history.values()), hide_index=True, width="stretch",
        column_config={
            "RP(억원)": st.column_config.NumberColumn(format="%.2f"),
            "EEV(억원)": st.column_config.NumberColumn(format="%.2f"),
            "VSS(%)": st.column_config.NumberColumn(format="%+.2f"),
            "풀이(초)": st.column_config.NumberColumn(format="%.1f"),
        },
    )
    if st.button("이력 지우기"):
        st.session_state.sto_history = {}
        st.rerun()


# ── 페이지 ───────────────────────────────────────────────────────────────────


def render() -> None:
    """Two-Stage 성능 비교 (테스트) 페이지."""
    get_settings()
    params = render_controls()
    with st.sidebar:
        st.toggle("큰 단위로 보기 (GW · 억원 · 천t)", value=True, key="compact_units")

    st.markdown(
        '<div class="vpp-topbar">'
        '<div class="vpp-title"><span class="vpp-logo">⚡</span>Two-Stage 성능 비교 (테스트)</div>'
        '<div class="vpp-subtitle">시나리오 생성 → Two-Stage 풀이 → RP · EV · EEV · WS 비교 '
        "(formulation.md 3장)</div></div>",
        unsafe_allow_html=True,
    )
    if params is None:
        st.info("사이드바에서 조건을 정하고 '실행'을 누른다. 기본 조건으로 30초 안팎 걸린다.")
        return

    url = logic.service_urls()["dispatch_api"]
    gen = {"seed": params["seed"], "n_scenarios": params["n_scenarios"]}
    scen_res = fetch_scenarios(url, json.dumps(gen, sort_keys=True))
    if not scen_res.ok:
        fetch_scenarios.clear()
        st.error(f"dispatch_api 에서 시나리오를 받지 못했다 ({scen_res.error}). "
                 f"(요청 주소: {url}{API_PREFIX}/scenarios)")
        return
    scenario_set = scen_res.data

    payload = json.dumps(stochastic_payload(params, scenario_set), sort_keys=True)
    timeout = request_timeout(
        params["time_limit_s"], len(scenario_set["scenarios"]), params["compute_ws"]
    )
    res = fetch_stochastic(url, payload, timeout)
    if not res.ok:
        fetch_stochastic.clear()  # 실패는 캐시하지 않는다
        st.error(f"dispatch_api 가 Two-Stage 를 풀지 못했다 ({res.error}). "
                 f"(요청 주소: {url}{API_PREFIX}/dispatch/stochastic)")
        return
    data = res.data
    summary = summarize(data, params["mip_gap_pct"] / 100)
    labels = {sc["scenario_id"]: sc["label"] for sc in scenario_set["scenarios"]}

    history = st.session_state.setdefault("sto_history", {})
    history[json.dumps(params, sort_keys=True)] = history_row(params, summary)

    with st.container(border=True):
        render_verdict(summary, res)
    left, right = st.columns(2)
    with left, st.container(border=True):
        render_problems(summary)
    with right, st.container(border=True):
        render_breakdown(data)
    with st.container(border=True):
        render_scenarios(data, labels)
    with st.container(border=True):
        render_commitment(data)
    with st.container(border=True):
        render_history()
