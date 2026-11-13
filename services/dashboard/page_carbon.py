"""탄소 감축 분석 화면. dispatch API 결과만 사용하며 목업으로 대체하지 않는다."""

from __future__ import annotations

import json

import pandas as pd
import plotly.express as px
import streamlit as st

import logic
from ui import get_settings

FUEL_NAMES = {
    "bituminous_coal": "유연탄", "anthracite": "무연탄",
    "lng": "LNG", "nuclear": "원자력", "hydro": "수력",
}


def plan_label(label: str) -> str:
    """API의 계획 식별자를 화면 설명으로 바꾼다."""
    fixed = {
        "cost_reference": "기준: 비용 최소",
        "minimum_carbon": "탄소 최소",
        "minimum_carbon_cost_tiebreak": "탄소 최소 · 비용 비교 완료",
    }
    if label.startswith("target_"):
        return f"감축 목표 {label[7:-3]}%"
    if label.startswith("frontier_"):
        return f"중간 감축 계획 {label[9:]}"
    return fixed.get(label, label)


def comparison_rows(data: dict) -> list[dict]:
    """실패한 점의 비용·배출은 빈칸으로 보존한다."""
    return [{
        "계획": plan_label(p["label"]), "상태": p["status"], "검증 통과": p["valid"],
        "배출량(tCO₂eq)": p["carbon_ton"], "운영비(원)": p["operating_cost_krw"],
        "탄소 감축(%)": p["reduction_pct"], "비용 증가(%)": p["cost_increase_pct"],
        "계산 시간(초)": p["elapsed_s"],
    } for p in data["points"]]


def render() -> None:
    """사용자가 실행할 때만 여러 급전 계획을 계산한다."""
    get_settings()
    st.title("탄소 감축 분석")
    st.caption("같은 전력 수요와 예비율을 유지하면서 탄소 감축에 따른 비용과 발전량을 비교합니다.")
    with st.form("carbon_analysis"):
        targets = st.multiselect(
            "비용 최소 계획 대비 감축 목표 (%)", [1, 3, 5, 10, 20], default=[5, 10])
        use_ess = st.checkbox("ESS 사용", value=True)
        submitted = st.form_submit_button("탄소 감축 계획 계산")
    if submitted:
        with st.spinner("같은 조건에서 비용 최소 계획과 탄소 감축 계획을 계산 중입니다…"):
            res = logic.call_api(
                "POST", f"{logic.service_urls()['dispatch_api']}{logic.API_PREFIX}/dispatch/pareto",
                payload={"reduction_targets_pct": targets, "use_ess": use_ess,
                         "ess_mode": "cost", "carbon_accounting": "lifecycle"},
                timeout=120,
            )
        if not res.ok:
            st.session_state.pop("carbon_analysis_result", None)
            st.error(f"탄소 분석을 완료하지 못했습니다: {res.error}")
        else:
            st.session_state["carbon_analysis_result"] = res.data
    data = st.session_state.get("carbon_analysis_result")
    if not data:
        st.info("계산을 실행하면 수집 데이터의 최신 연속 24시간을 기준으로 비교합니다.")
        return
    ess_used = bool(data.get("reference") and data["reference"].get("ess"))
    st.caption(f"ESS {'사용' if ess_used else '미사용'} 조건으로 계산한 결과입니다.")
    st.caption(f"마지막 실행 결과 · {data['timestamps'][0]} ~ {data['timestamps'][-1]} · "
               f"{data['elapsed_s']:.2f}초 · 상태: {data['state']}")
    if data["state"] != "complete":
        st.warning("일부 계획의 계산 또는 검증이 완료되지 않았습니다. 상태와 진단을 확인하세요.")
    st.caption("급전 가능 발전원 전과정 배출 추정치입니다. "
               "고정 재생발전과 별도 기동 배출은 제외됩니다.")
    st.dataframe(pd.DataFrame(comparison_rows(data)), hide_index=True, width="stretch")
    if data["maximum_reduction_pct"] is not None:
        st.metric("현재 조건의 최대 감축 가능률", f"{data['maximum_reduction_pct']:.2f}%")
    if data["frontier"]:
        frame = pd.DataFrame(data["frontier"])
        frame["label"] = frame["label"].map(plan_label)
        chart = px.scatter(frame, x="carbon_ton", y="operating_cost_krw", hover_name="label",
                           color="optimal", labels={"carbon_ton": "배출량(tCO₂eq)",
                           "operating_cost_krw": "운영비(원)", "optimal": "최적성 확인"})
        st.plotly_chart(chart, width="stretch")
    valid = {p["label"]: p for p in data["points"] if p["valid"]}
    if valid:
        label = st.selectbox("운전 계획 상세", list(valid), format_func=plan_label)
        selected = valid[label]
        ref = data["reference"]
        if ref and ref["valid"]:
            fuels = sorted(set(ref["fuel_energy_mwh"]) | set(selected["fuel_energy_mwh"]))
            st.dataframe(pd.DataFrame([{
                "발전원": FUEL_NAMES.get(f, f),
                "기준 발전량(MWh)": ref["fuel_energy_mwh"].get(f, 0),
                "선택 발전량(MWh)": selected["fuel_energy_mwh"].get(f, 0),
                "변화(MWh)": selected["fuel_energy_mwh"].get(f, 0)
                - ref["fuel_energy_mwh"].get(f, 0),
            } for f in fuels]), hide_index=True, width="stretch")
        operations = pd.DataFrame(selected["operations"]).rename(columns={
            "unit_id": "발전기", "fuel": "발전원", "energy_mwh": "발전량(MWh)",
            "on_hours": "가동 시간(h)", "startups": "기동 횟수", "p_mw": "시간별 출력(MW)",
        })
        operations["발전원"] = operations["발전원"].map(lambda f: FUEL_NAMES.get(f, f))
        st.dataframe(operations, hide_index=True, width="stretch")
        if selected["ess"]:
            ess = selected["ess"]
            st.write(f"ESS 충전 {ess['energy_charged_mwh']:,.1f} MWh · "
                     f"방전 {ess['energy_discharged_mwh']:,.1f} MWh · "
                     f"손실 {ess['loss_mwh']:,.1f} MWh")
        st.dataframe(pd.DataFrame(selected["checks"]), hide_index=True, width="stretch")
    with st.expander("산정 계수와 진단"):
        st.json({"basis": data["accounting_basis"], "unit": data["factor_unit"],
                 "factors": data["factors"], "notes": data["notes"]})
    st.download_button("분석 결과 JSON 다운로드", data=json.dumps(
        data, ensure_ascii=False, indent=2),
        file_name="carbon_analysis.json", mime="application/json")
