"""발전원 조합별 VSS 측정 스크립트 (D, 2026-11-25).

"내일 어떤 발전원을 켜 둘 수 있느냐에 따라 VSS 가 오르는지 내리는지"를 본다. Two-Stage 모델은
API 기본값 그대로 두고, 쓸 발전원 조합과 시나리오만 바꾼다. 결과 JSON 은 대시보드
Two-Stage 탭(services/dashboard/page_stochastic_test.py)이 그대로 읽는다.

시나리오 (무작위 없음, 10개)
    계획일의 forecast_api Day-ahead 예측을 받아 수요만 x (1 ± 0.02k), k = 1..5 로 흔든다
    (+2·4·6·8·10%, −2·4·6·8·10%, 확률 각 0.1). 태양광·풍력은 날씨로 예측한 값 그대로 —
    켜고 끄는 대상이 아니라 수요에서 빼는 공급이다. 재생 예측이 빗나가는 경우는 다루지 않는다.

발전원 조합
    원자력 / 석탄(유연탄 + 무연탄) / LNG / 수력 의 공집합 뺀 15개. 첫 번째(전체)가 기준이다.
    10개 시나리오를 모든 조합에 똑같이 넣어, VSS 차이가 발전원 구성에서만 나게 한다.

사용법 (레포 루트에서, collector·forecast_api 가 떠 있어야 한다):
    python scripts/measure_vss_mix.py                     # 15 조합
    python scripts/measure_vss_mix.py --combos 1          # 기준(전체)만, 시간 확인용
    python scripts/measure_vss_mix.py --json services/dashboard/vss_mix.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services" / "dispatch_api"))
sys.path.insert(0, str(REPO_ROOT))

import forecast_client  # noqa: E402

import scenarios  # noqa: E402
import stochastic  # noqa: E402
import units  # noqa: E402
from shared.schemas import Scenario, ScenarioProfile, SolverOptions, UnitHour  # noqa: E402

# 그룹 이름 → generators.yaml 의 fuel. 무연탄은 400MW 라 따로 두면 조합만 늘어 석탄에 묶는다
GROUPS: dict[str, tuple[str, ...]] = {
    "원자력": ("nuclear",),
    "석탄": ("bituminous_coal", "anthracite"),
    "LNG": ("lng",),
    "수력": ("hydro",),
}
STEPS = (1, 2, 3, 4, 5)   # 단계 k
STEP_PCT = 2.0            # 단계당 % (k=5 → ±10%)
DEFAULT_DATE = date(2026, 10, 28)  # 세 target 데이터가 모두 있는 최신 계획일 (2026-11-25 기준)
TIME_LIMIT_S = 120.0
# API 기본(stochastic.yaml request_defaults, 0.5%)보다 훨씬 작게 둔다. 0.5% 로 풀면 평균 계획(EV)이
# 최적이 아닌 해로 끝나 EEV·VSS 가 크게 흔들렸다 (2026-11-26 진단: 석탄+LNG+수력 EV 비용 0.3% 차이에
# EEV 224억 차이, VSS 8.28% ↔ 19.43%). 조합끼리 비교하려면 거의 최적 해여야 한다.
# 기준: 허용오차 ≤ 구분하려는 가장 작은 VSS 차이(0.1%p) ÷ 10 = 0.01% (HiGHS 기본값과 같다).
MIP_GAP = 1e-4


# ── 시나리오 ─────────────────────────────────────────────────────────────────


def demand_scenarios(base: ScenarioProfile) -> list[Scenario]:
    """수요만 ±2k% 로 흔든 10개 (낮은 쪽부터). 태양광·풍력은 예측 그대로, 확률 각 0.1."""
    demand = np.array(base.demand_mw, dtype=float)
    levels = [-k for k in reversed(STEPS)] + list(STEPS)
    return [
        Scenario(
            scenario_id=f"D{k * STEP_PCT:+03.0f}",
            probability=1 / len(levels),
            demand_mw=list(demand * (1 + k * STEP_PCT / 100)),
            solar_mw=list(base.solar_mw),
            wind_mw=list(base.wind_mw),
        )
        for k in levels
    ]


# ── 발전원 조합 ──────────────────────────────────────────────────────────────


def combos() -> list[tuple[str, ...]]:
    """그룹 조합 15개. 큰 조합부터 — 첫 번째(4개 전체)가 기준이다."""
    names = tuple(GROUPS)
    return [c for size in range(len(names), 0, -1) for c in itertools.combinations(names, size)]


def unit_ids(combo: tuple[str, ...], fuel_of: dict[str, str]) -> list[str]:
    """조합에 든 그룹의 유닛 id (generators.yaml 순서)."""
    fuels = {f for g in combo for f in GROUPS[g]}
    return [uid for uid, fuel in fuel_of.items() if fuel in fuels]


def group_capacity(all_units: list, renewable_caps: dict[str, float]) -> dict[str, float]:
    """발전원별 설비용량 (MW). 대시보드가 발전원 옆 배터리(출력 / 설비)를 그릴 때 쓴다."""
    out = {g: sum(u.p_max_mw for u in all_units if u.fuel in fuels) for g, fuels in GROUPS.items()}
    return {**out, **renewable_caps}


def renewable_caps() -> dict[str, float]:
    """태양광·풍력 설비용량 (stochastic.yaml scenario_generator)."""
    return {"태양광": scenarios.CFG["solar_capacity_mw"], "풍력": scenarios.CFG["wind_capacity_mw"]}


def is_short(capacity_mw: float, peak_net_mw: float) -> bool:
    """설비 합 < 시나리오 최대 순수요. 이때 VSS 는 공급 부족 페널티로 채워져 비교 의미가 없다."""
    return capacity_mw < peak_net_mw


def group_plan(first_stage: list[UnitHour], fuel_of: dict[str, str],
               horizon_h: int) -> dict[str, dict]:
    """전날 계획을 그룹별 시간 배열로: 켠 유닛 수와 계획 출력(시나리오 출력의 기대값, MW)."""
    group_of = {fuel: g for g, fuels in GROUPS.items() for fuel in fuels}
    start = min(u.timestamp for u in first_stage) if first_stage else None
    plan = {g: {"units": 0, "on": [0] * horizon_h, "mw": [0.0] * horizon_h} for g in GROUPS}
    seen = set()
    for u in first_stage:
        g = group_of[fuel_of[u.unit_id]]
        t = int((u.timestamp - start).total_seconds() // 3600)
        if u.unit_id not in seen:
            seen.add(u.unit_id)
            plan[g]["units"] += 1
        plan[g]["on"][t] += int(u.on)
        plan[g]["mw"][t] += u.p_mw
    return plan


# ── 정리 ─────────────────────────────────────────────────────────────────────


def summarize(rows: list[dict]) -> list[dict]:
    """각 행에 기준(전체) 대비 VSS 차이 delta (%p) 를 붙인다. 기준을 못 풀었으면 None."""
    full = combos()[0]
    base = next((r for r in rows if r["combo"] == full and r["status"] != "failed"), None)
    out = []
    for r in rows:
        ok = base is not None and r["status"] != "failed" and r["vss_pct"] is not None
        out.append({**r, "delta": r["vss_pct"] - base["vss_pct"] if ok else None})
    return out


def rank(summary: list[dict]) -> tuple[list[dict], list[dict]]:
    """(공급 충분하고 delta 가 있는 조합, delta 내림차순 — 기준 제외), (공급 부족·풀이 실패)."""
    full = combos()[0]
    excluded = [r for r in summary if r["short"] or r["status"] == "failed"]
    ranked = [r for r in summary
              if r not in excluded and r["combo"] != full and r["delta"] is not None]
    return sorted(ranked, key=lambda r: r["delta"], reverse=True), excluded


# ── 측정 ─────────────────────────────────────────────────────────────────────


def base_forecast(plan_day: date) -> ScenarioProfile:
    """계획일의 Day-ahead 기준 예측. 그 날짜가 아니면 멈춘다 (config 로 폴백하지 않는다)."""
    now = datetime(plan_day.year, plan_day.month, plan_day.day, tzinfo=forecast_client.KST)
    base = forecast_client.latest_base(now)
    if base.start is None or base.start.date() != plan_day:
        raise SystemExit(f"forecast_api 기준 예측의 계획일이 {plan_day} 가 아니다: {base.start}")
    return base


def solve_combo(combo: tuple[str, ...], all_units: list, picked: list[Scenario],
                start: datetime, peak_net: float) -> dict:
    fuel_of = {u.id: u.fuel for u in all_units}
    ids = unit_ids(combo, fuel_of)
    chosen = [u for u in all_units if u.id in ids]
    capacity = sum(u.p_max_mw for u in chosen)
    row = {"combo": combo, "capacity_mw": capacity, "short": is_short(capacity, peak_net),
           "status": "failed", "vss_pct": None, "bound_pct": None, "shed_mwh": None,
           "rp_won": None, "eev_won": None, "ws_won": None, "cost": None,
           "plan": None, "scenarios": None, "error": None}
    d = stochastic.load_request_defaults()
    t0 = time.perf_counter()
    try:
        r = stochastic.run(chosen, picked, stochastic.load_params(), start,
                           reserve_ratio=d.reserve_ratio, reserve_mw=d.reserve_mw,
                           shed_penalty=d.shed_penalty_won_per_mwh,
                           opts=SolverOptions(time_limit_s=TIME_LIMIT_S, mip_gap=MIP_GAP),
                           compute_ws=True)
        v = r.vss
        row.update(
            status=r.status, vss_pct=v.vss_pct, rp_won=v.rp_won, eev_won=v.eev_won,
            ws_won=v.ws_won,
            # 상한: RP ≥ WS 이므로 VSS = EEV − RP ≤ EEV − WS
            bound_pct=(v.eev_won - v.ws_won) / v.eev_won * 100 if v.ws_won else None,
            shed_mwh=sum(s.shed_mwh * s.probability for s in r.scenarios),
            cost=r.expected_cost.model_dump(),
            plan=group_plan(r.first_stage, fuel_of, r.horizon_h),
            scenarios=[{"id": s.scenario_id, "probability": s.probability,
                        "total_won": s.cost.total_won, "shed_mwh": s.shed_mwh}
                       for s in r.scenarios],
        )
    except RuntimeError as e:  # RP·EV·EEV 중 하나를 못 풀었다
        row["error"] = str(e)
    row["seconds"] = round(time.perf_counter() - t0, 1)
    return row


def measure(plan_day: date, n_combos: int | None = None) -> dict:
    base = base_forecast(plan_day)
    picked = demand_scenarios(base)
    peak_net = max(float(stochastic.net_load(s).max()) for s in picked)
    print(f"계획일 {plan_day}, 기준 예측 {base.source} (데이터 {base.issued_at} 까지)", flush=True)
    all_units = units.load_units()

    rows = []
    for combo in combos()[:n_combos]:
        row = solve_combo(combo, all_units, picked, base.start, peak_net)
        rows.append(row)
        print(_line(row), flush=True)

    return {
        "plan_day": plan_day.isoformat(),
        "start": base.start.isoformat(),
        "base_issued_at": base.issued_at.isoformat() if base.issued_at else None,
        "groups": {g: list(f) for g, f in GROUPS.items()},
        "group_capacity_mw": group_capacity(all_units, renewable_caps()),
        "base": {"demand_mw": base.demand_mw, "solar_mw": base.solar_mw, "wind_mw": base.wind_mw},
        "scenarios": [{"id": s.scenario_id, "probability": s.probability,
                       "demand_mw": s.demand_mw} for s in picked],
        "rows": rows,
    }


def _fmt(v: float | None, spec: str = ".2f") -> str:
    return "-" if v is None else format(v, spec)


def _line(r: dict) -> str:
    name = "+".join(r["combo"])
    short = " 공급 부족" if r["short"] else ""
    return (f"  {name:<18} {r['capacity_mw'] / 1e3:5.1f}GW  {r['status']:<10} "
            f"VSS {_fmt(r['vss_pct']):>6}%  상한 {_fmt(r['bound_pct']):>6}%  "
            f"부하차단 {_fmt(r['shed_mwh'], ',.0f'):>7}MWh  {r['seconds']:5.1f}s{short}")


def report(result: dict) -> None:
    summary = summarize(result["rows"])
    ranked, excluded = rank(summary)
    print("\n결론")
    if ranked:
        top, bottom = ranked[0], ranked[-1]
        print(f"  가장 많이 오른 조합: {'+'.join(top['combo'])} ({top['delta']:+.2f}%p)")
        print(f"  가장 많이 내린 조합: {'+'.join(bottom['combo'])} ({bottom['delta']:+.2f}%p)")
    else:
        print("  기준(전체)을 풀지 못했거나 비교할 조합이 없다")
    if excluded:
        print("  제외 (공급 부족·풀이 실패): " + ", ".join("+".join(r["combo"]) for r in excluded))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", type=date.fromisoformat, default=DEFAULT_DATE)
    parser.add_argument("--combos", type=int, default=None, help="앞에서 N 개 조합만 (1 = 기준만)")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    # 로컬에서 docker compose 서비스를 부른다 (이미 정했으면 그대로)
    os.environ.setdefault("FORECAST_URL", "http://localhost:8001")
    os.environ.setdefault("COLLECTOR_URL", "http://localhost:8000")

    result = measure(args.date, args.combos)
    report(result)
    if args.json:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str),
                             encoding="utf-8")
        print(f"\n저장: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
