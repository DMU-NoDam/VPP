"""Two-Stage · VSS 페이지 — 발전원 조합 15개 중 하나를 누르면 그 조합대로 발전 단지에 불이 켜진다.

문제 (formulation.md 8장, 2026-11-25 D 정리)
    내일 수요를 시나리오 10개(예측의 ±2·4·6·8·10%, 확률 각 0.1)로 나누고, 전날 어떤 발전기를
    켜 둘지 정한다. 출력은 당일 실제 수요를 보고 정하며, 화면의 출력은 10개 시나리오 출력의
    기대값이다. 태양광·풍력은 날씨로 예측한 값 그대로 들어오는 공급이다 (켜고 끄지 않는다).

조합 목록·3D 단지·지표 패널은 한 화면(scene3d.html)에 넣는다. 조합·시간대·수요를 눌러도 페이지를
다시 그리지 않고 화면 안에서 불이 서서히 켜지고 꺼지게 하려고, 15 조합의 값을 미리 묶어 넘긴다
(vss_mix.view_model). 결과는 scripts/measure_vss_mix.py 가 미리 계산한 vss_mix.json 이다.
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st
import vss_mix as mix

SCENE = Path(__file__).with_name("scene3d.html")
# three.js r128 (MIT). CDN 대신 이 파일들을 HTML 에 넣어 인터넷 없이도 3D 가 뜨게 한다
VENDOR = Path(__file__).with_name("vendor")
VENDOR_FILES = ("three.min.js", "OrbitControls.js")
APP_HEIGHT = 860  # scene3d.html 의 #app 높이(840) + 여백


@st.cache_data(ttl=600)
def load_data() -> dict | None:
    """측정 결과 (파일이 바뀌면 10분 안에 다시 읽는다)."""
    return mix.load()


@st.cache_data
def scene_template() -> str:
    html = SCENE.read_text(encoding="utf-8")
    for name in VENDOR_FILES:
        html = html.replace(f"/*__VENDOR:{name}__*/", (VENDOR / name).read_text(encoding="utf-8"))
    return html


def scene_html(payload: dict) -> str:
    """화면 HTML 에 값을 넣는다. "</" 를 막아 값이 script 태그를 닫지 못하게 한다."""
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return scene_template().replace("__DATA__", data)


def render_comparison(data: dict) -> None:
    """15 조합 비교표 (펼쳐 둔다)."""
    frame = mix.comparison_frame(data)
    up, down = mix.extremes(frame)
    with st.expander("발전원 조합 15개 비교표", icon=":material/table_chart:", expanded=True):
        if up is not None:
            col = "기준 대비(%p)"
            first = ("VSS 가 **가장 많이 오른** 조합" if up[col] > 0
                     else "전체 켬보다 VSS 가 오른 조합은 없다. **가장 적게 내린** 조합")
            st.markdown(
                f"- {first}: **{up['조합']}** ({up[col]:+.1f}%p)\n"
                f"- VSS 가 **가장 많이 내린** 조합: **{down['조합']}** ({down[col]:+.1f}%p)"
            )
        st.dataframe(
            frame, hide_index=True,
            column_config={
                "설비(GW)": st.column_config.NumberColumn(format="%.1f"),
                "VSS(%)": st.column_config.NumberColumn(format="%.2f"),
                "기준 대비(%p)": st.column_config.NumberColumn(format="%+.2f"),
                "기대 비용(억원)": st.column_config.NumberColumn(format="%,.0f"),
                "비용 차이(억원)": st.column_config.NumberColumn(format="%+,.0f"),
            },
        )
        st.caption("공급 부족 조합은 설비가 내일 최대 순수요보다 작아 VSS 가 정전 비용에서 나온다. "
                   "발전원을 빼서 비용이 줄지는 않는다 (모델이 안 켜는 쪽을 고를 수 있으므로).")


def render() -> None:
    data = load_data()
    if data is None:
        st.warning("측정 결과(vss_mix.json)가 없습니다. 레포 루트에서 "
                   "`python scripts/measure_vss_mix.py --json services/dashboard/vss_mix.json` "
                   "을 실행하세요.", icon=":material/hourglass_empty:")
        return

    # alt 인자는 Streamlit 1.67+ 에만 있다 (requirements 는 >=1.35) → 쓰지 않는다
    st.iframe(scene_html(mix.view_model(data)), height=APP_HEIGHT)
    render_comparison(data)
    st.caption(
        f"계획일 {data['plan_day']} (forecast_api Day-ahead, "
        f"데이터 {data['base_issued_at']} 까지) · "
        "수요 시나리오 10개 = 예측의 ±2·4·6·8·10%, 확률 각 0.1 · 태양광·풍력은 날씨 예측값 · "
        "전날 켜고 끄기를 정하고 출력은 시나리오 기대값 · scripts/measure_vss_mix.py 로 계산"
    )
