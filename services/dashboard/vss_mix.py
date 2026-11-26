"""Two-Stage 탭 데이터 — 발전원 조합별 VSS 측정 결과를 화면 값으로 바꾼다.

결과는 scripts/measure_vss_mix.py 가 만든 JSON(이 폴더의 vss_mix.json)이다. 15 조합을 화면에서
그때그때 풀면 조합당 20초 넘게 걸려 3D 조작이 끊기므로, 미리 계산한 스냅샷을 읽는다.

시간대는 둘로 묶어 보여 준다 (계산은 시간별 24개 그대로).
    낮      07~18시 [가정] 태양광이 나오는 시간
    저녁·밤 나머지
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

DATA_PATH = Path(__file__).with_name("vss_mix.json")

GROUP_ORDER = ("원자력", "석탄", "LNG", "수력")
RENEWABLES = ("태양광", "풍력")
# app.py FUEL_COLOR 와 같은 색 (석탄 = coal)
COLOR = {
    "원자력": "#a78bfa", "석탄": "#64748b", "LNG": "#fb923c", "수력": "#3b82f6",
    "태양광": "#facc15", "풍력": "#2dd4bf",
}
DAY = "낮"
NIGHT = "저녁·밤"
DAY_HOURS = tuple(range(7, 19))
EXPECTED = "평균"  # 시나리오 선택지: 확률 가중 평균(= 기준 예측)


def load(path: Path = DATA_PATH) -> dict | None:
    """측정 결과 JSON. 없으면 None."""
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def hours(block: str, horizon_h: int = 24) -> list[int]:
    """시간대 → 시(時) 목록."""
    if block == DAY:
        return [h for h in DAY_HOURS if h < horizon_h]
    return [h for h in range(horizon_h) if h not in DAY_HOURS]


def _mean(values: list[float], idx: list[int]) -> float:
    return sum(values[i] for i in idx) / len(idx) if idx else 0.0


def find_row(data: dict, selected: Iterable[str]) -> dict | None:
    """켠 그룹 집합과 같은 조합의 결과 행. 아무것도 안 켰으면 None."""
    want = set(selected)
    return next((r for r in data["rows"] if set(r["combo"]) == want), None)


def baseline(data: dict) -> dict | None:
    """기준 = 4개 전체를 켠 조합."""
    return find_row(data, GROUP_ORDER)


def scenario_label(scenario_id: str) -> str:
    """"D+06" → "+6%"."""
    return f"{int(scenario_id[1:]):+d}%"


def scenario_options(data: dict) -> list[str]:
    """선택지: 낮은 수요부터, 가운데에 평균."""
    labels = [scenario_label(s["id"]) for s in data["scenarios"]]
    low = [x for x in labels if x.startswith("-")]
    high = [x for x in labels if x.startswith("+")]
    return low + [EXPECTED] + high


def scenario_demand(data: dict, choice: str) -> list[float]:
    """선택한 시나리오의 시간별 수요. 평균이면 기준 예측."""
    if choice == EXPECTED:
        return list(data["base"]["demand_mw"])
    s = next(s for s in data["scenarios"] if scenario_label(s["id"]) == choice)
    return list(s["demand_mw"])


def scenario_result(row: dict | None, choice: str) -> dict | None:
    """선택한 시나리오의 비용·부하차단. 평균이거나 결과가 없으면 None."""
    if row is None or not row.get("scenarios") or choice == EXPECTED:
        return None
    return next((s for s in row["scenarios"] if scenario_label(s["id"]) == choice), None)


def block_supply(data: dict, row: dict | None, block: str) -> dict[str, float]:
    """시간대 평균 공급 (MW): 발전원 그룹별 계획 출력 + 태양광·풍력 예측."""
    idx = hours(block, len(data["base"]["demand_mw"]))
    out = {g: 0.0 for g in GROUP_ORDER}
    if row is not None and row.get("plan"):
        for g in GROUP_ORDER:
            out[g] = _mean(row["plan"][g]["mw"], idx)
    out["태양광"] = _mean(data["base"]["solar_mw"], idx)
    out["풍력"] = _mean(data["base"]["wind_mw"], idx)
    return out


def status(row: dict) -> str:
    """정상 / 공급 부족 / 풀이 실패."""
    if row["status"] == "failed" or row["vss_pct"] is None:
        return "풀이 실패"
    return "공급 부족" if row["short"] else "정상"


def _plan_block(data: dict, row: dict, block: str) -> dict[str, dict]:
    """시간대 하나의 발전원별 {평균 출력 MW, 켠 유닛 수(최대), 유닛 수}."""
    idx = hours(block, len(data["base"]["demand_mw"]))
    plan = row.get("plan") or {}
    supply = block_supply(data, row, block)
    out = {}
    for g in GROUP_ORDER:
        p = plan.get(g)
        out[g] = {"mw": supply[g], "on": max(p["on"][i] for i in idx) if p else 0,
                  "units": p["units"] if p else 0}
    for g in RENEWABLES:
        out[g] = {"mw": supply[g], "on": int(supply[g] > 1.0), "units": 0}
    return out


def view_model(data: dict) -> dict:
    """화면(조합 목록·3D·지표)에 넘길 값을 한 번에 묶는다.

    조합·시간대·수요를 눌러 바꿀 때 페이지를 다시 그리지 않고 화면 안에서 바로 바뀌도록,
    15 조합 x 시간대 2개의 공급과 시나리오별 비용·부하차단을 미리 넣어 둔다.
    목록 순서: 정상 조합(VSS 큰 순) → 공급 부족(설비 큰 순) → 풀이 실패.
    """
    n = len(data["base"]["demand_mw"])
    base = baseline(data)
    base_ok = base is not None and status(base) != "풀이 실패"
    options = scenario_options(data)
    blocks = (DAY, NIGHT)
    renew = [s + w for s, w in zip(data["base"]["solar_mw"], data["base"]["wind_mw"], strict=True)]
    peak_net = max(d - r for s in data["scenarios"]
                   for d, r in zip(s["demand_mw"], renew, strict=True))

    combos = []
    for r in data["rows"]:
        st_ = status(r)
        ok = st_ != "풀이 실패"
        combos.append({
            "key": " + ".join(r["combo"]),
            "groups": list(r["combo"]),
            "capacity_mw": r["capacity_mw"],
            "status": st_,
            "vss": r["vss_pct"] if ok else None,
            "bound": r.get("bound_pct") if ok else None,
            "delta": r["vss_pct"] - base["vss_pct"] if ok and base_ok else None,
            "cost_eok": r["rp_won"] / 1e8 if ok else None,
            "cost_delta_eok": (r["rp_won"] - base["rp_won"]) / 1e8 if ok and base_ok else None,
            "supply": {b: _plan_block(data, r, b) for b in blocks} if ok else None,
            "scen": {scenario_label(s["id"]): {"cost_eok": s["total_won"] / 1e8,
                                                "shed_mwh": s["shed_mwh"]}
                     for s in r.get("scenarios") or []},
        })
    rank = {"정상": 0, "공급 부족": 1, "풀이 실패": 2}
    combos.sort(key=lambda c: (rank[c["status"]],
                               -(c["vss"] or 0) if c["status"] == "정상" else -c["capacity_mw"]))
    return {
        "plan_day": data["plan_day"],
        "issued_at": data.get("base_issued_at"),
        "groups": list(GROUP_ORDER),
        "renewables": list(RENEWABLES),
        "colors": COLOR,
        "blocks": list(blocks),
        "scenarios": options,
        "expected": EXPECTED,
        "demand": {b: {c: _mean(scenario_demand(data, c), hours(b, n)) for c in options}
                   for b in blocks},
        "peak_net_mw": peak_net,
        # 발전원별 설비용량 (MW) — 발전원 옆 배터리 = 출력 / 설비. 없으면 빈 dict
        "capacity_mw": dict(data.get("group_capacity_mw") or {}),
        # 24시간 곡선 (3D 물결선): 시나리오별 수요, 태양광·풍력 예측
        "hourly": {
            "demand": {c: scenario_demand(data, c) for c in options},
            "solar": list(data["base"]["solar_mw"]),
            "wind": list(data["base"]["wind_mw"]),
            "day_hours": list(DAY_HOURS),
        },
        "baseline": " + ".join(GROUP_ORDER),
        "combos": combos,
    }


def comparison_frame(data: dict) -> pd.DataFrame:
    """15 조합 비교표. VSS·기대 비용을 기준(전체) 대비로 같이 둔다."""
    base = baseline(data)
    rows = []
    for r in data["rows"]:
        ok = r["status"] != "failed" and r["vss_pct"] is not None
        cost = r["rp_won"] / 1e8 if ok else None
        base_ok = base is not None and base["vss_pct"] is not None
        rows.append({
            "조합": " + ".join(r["combo"]),
            "설비(GW)": r["capacity_mw"] / 1e3,
            "VSS(%)": r["vss_pct"] if ok else None,
            "기준 대비(%p)": r["vss_pct"] - base["vss_pct"] if ok and base_ok else None,
            "기대 비용(억원)": cost,
            "비용 차이(억원)": cost - base["rp_won"] / 1e8 if ok and base_ok else None,
            "상태": status(r),
        })
    return pd.DataFrame(rows)


def extremes(frame: pd.DataFrame) -> tuple[pd.Series | None, pd.Series | None]:
    """(VSS 가 가장 많이 오른 조합, 가장 많이 내린 조합) — 정상 조합 중, 기준 제외."""
    ok = frame[(frame["상태"] == "정상") & (frame["조합"] != " + ".join(GROUP_ORDER))]
    ok = ok.dropna(subset=["기준 대비(%p)"])
    if ok.empty:
        return None, None
    ok = ok.sort_values("기준 대비(%p)", ascending=False)
    return ok.iloc[0], ok.iloc[-1]
