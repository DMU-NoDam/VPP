"""최적화 코어 KPI 측정 스크립트.

CLAUDE.md 정량 기준 중 C 담당 항목을 **반복 측정 가능하게** 만든다. 수동 계산이
아니라 이 스크립트로 재현한다.

    비용 절감률 vs Rule-based   >= 10%
    MILP 응답시간               <= 10초
    ESS 피크 감소율             >= 5%
    정상 해에서 제약 위반       0건

창마다 네 번 푼다.
    rule        Rule-based baseline (ESS 없음)
    milp        ESS 없는 MILP        -> 비용 절감률의 **공정 비교** 대상
    milp+ess    ESS cost 모드        -> 비용 최소화가 부수적으로 깎는 피크
    milp+shave  ESS peak_shaving 모드 -> 피크 감소율 5% KPI 판정

사용법 (레포 루트에서):
    python scripts/measure_dispatch_kpi.py
    python scripts/measure_dispatch_kpi.py --windows 14 --horizon 24 --json out.json

수집 데이터의 최신 연속 구간을 24시간씩 뒤로 밀며 여러 창(window)에서 측정한다.
한 창만 보면 그날이 쉬웠는지 어려웠는지 알 수 없다 (기간 대표성).
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services" / "dispatch_api"))
sys.path.insert(0, str(REPO_ROOT))

from dataclasses import replace  # noqa: E402

import baseline  # noqa: E402
import carbon  # noqa: E402
import milp  # noqa: E402
import validation  # noqa: E402
from loaders.config_loader import (  # noqa: E402
    load_ess,
    load_generator_specs,
    load_settings,
    resolve_fuel_costs,
)
from loaders.data_loader import (  # noqa: E402
    DEMAND_INTERVAL_MIN,
    TRADING_INTERVAL_MIN,
    build_net_demand,
    read_csv_dataset,
    to_hourly,
    wide_trading,
)
from models import DispatchInput  # noqa: E402
from schedule import cost_reduction_pct  # noqa: E402

COST_REDUCTION_TARGET_PCT = 10.0
RUNTIME_TARGET_S = 10.0
PEAK_REDUCTION_TARGET_PCT = 5.0


def load_net_demand(settings, csv_dir: Path):
    """1시간 순수요 시계열 전체 (측정용이라 horizon 으로 자르지 않는다)."""
    demand = to_hourly(
        read_csv_dataset(csv_dir, "power_demand"), "ts", ["demand_mw"],
        timezone=settings.timezone, resample=settings.resample,
        source_interval_min=DEMAND_INTERVAL_MIN,
        min_sample_ratio=settings.min_sample_ratio,
    )
    wide = wide_trading(read_csv_dataset(csv_dir, "trading_by_fuel"))
    fuel_columns = [c for c in wide[0] if c != "ts"]
    trading = to_hourly(
        wide, "ts", fuel_columns,
        timezone=settings.timezone, resample=settings.resample,
        source_interval_min=TRADING_INTERVAL_MIN,
        min_sample_ratio=settings.min_sample_ratio,
    )
    index = demand.index.intersection(trading.index)
    return build_net_demand(demand.loc[index], trading.loc[index], settings.net_demand_mode)


def windows(net_demand, horizon_h: int, count: int, align_hour: int | None = None):
    """끝에서부터 horizon 시간씩, 시각이 연속인 창만 고른다.

    `align_hour` 를 주면 그 시각(보통 0시)에 시작하는 창만 쓴다.

    왜 정렬이 필요한가: ESS 의 SOC 정책(초기 = 종료 = 50%)은 **하루 단위**로
    정의돼 있고, day-ahead 급전 계획의 horizon 도 달력상 하루다. 정렬하지 않으면
    16시에 시작하는 창이 생기고, 그 창에서는 그날 저녁 피크가 horizon 의 1~2번째
    시간에 온다. 피크 전에 충전할 시간이 없으니 ESS 가 피크를 못 깎는데, 이건
    ESS 의 한계가 아니라 창을 자른 방식의 문제다.
    두 숫자를 모두 측정해 비교하는 게 정직하므로 옵션으로 둔다.
    """
    out = []
    end = len(net_demand)
    while len(out) < count and end - horizon_h >= 0:
        chunk = net_demand.iloc[end - horizon_h:end]
        gaps = (chunk.index[1:] - chunk.index[:-1]).unique()
        contiguous = len(gaps) == 1 and gaps[0].total_seconds() == 3600
        aligned = align_hour is None or chunk.index[0].hour == align_hour
        if contiguous and aligned:
            out.append(chunk)
            end -= horizon_h
        else:
            # 정렬을 맞추려면 창을 1시간씩 밀어 본다
            end -= horizon_h if align_hour is None else 1
    return out


def measure(args) -> dict:
    config_dir = REPO_ROOT / args.config_dir
    csv_dir = REPO_ROOT / args.csv_dir

    settings = load_settings(config_dir / "settings.yaml")
    specs = load_generator_specs(config_dir / "generators.yaml")
    generators = resolve_fuel_costs(
        specs, read_csv_dataset(csv_dir, "fuel_cost"), settings.fuel_cost_month
    )
    solver_config = milp.SolverConfig.from_settings(settings)
    ess_base = None if args.no_ess else load_ess(config_dir / "ess.yaml")
    net_demand = load_net_demand(settings, csv_dir)
    chunks = windows(net_demand, args.horizon, args.windows, args.align_hour)

    align = "자정 정렬" if args.align_hour == 0 else (
        "정렬 없음" if args.align_hour is None else f"{args.align_hour}시 정렬")
    print(f"발전기 {len(generators)}기 · horizon {args.horizon}h · 창 {len(chunks)}개 "
          f"({align}) · solver {solver_config.name}")
    if ess_base is not None:
        print(f"ESS {ess_base.power_discharge_mw:,.0f} MW / {ess_base.capacity_mwh:,.0f} MWh "
              f"· 왕복효율 {ess_base.round_trip_efficiency:.2f} "
              f"· 목표 피크 감소 {ess_base.peak_target_pct:.0f}%")
    print(f"{'window 시작':<22}{'baseline억':>11}{'MILP억':>10}{'절감%':>7}"
          f"{'+ESS절감%':>10}{'피크%cost':>10}{'피크%shave':>11}{'solve s':>9}{'위반':>5}")

    rows = []
    for chunk in reversed(chunks):
        inp = DispatchInput(
            generators=generators,
            timestamps=[ts.to_pydatetime() for ts in chunk.index],
            net_demand_mw=[float(v) for v in chunk.to_numpy()],
            reserve_margin_ratio=settings.reserve_margin_ratio,
            storage_rate_pct=None,
        )
        common = dict(solver_config=solver_config, hydro_policy=settings.hydro_policy)

        started = time.perf_counter()
        optimized = milp.solve(inp, **common)
        wall_s = time.perf_counter() - started
        rule = baseline.run(inp, hydro_policy=settings.hydro_policy,
                            shed_penalty_krw_per_mwh=0.0)

        ess_cost = ess_shave = None
        if ess_base is not None:
            ess_cost = milp.solve(inp, ess=replace(ess_base, mode="cost"), **common)
            ess_shave = milp.solve(
                inp,
                ess=replace(ess_base, mode="peak_shaving",
                            peak_target_pct=args.peak_target),
                **common,
            )

        milp_violations = validation.all_violations(optimized, generators)
        rule_violations = validation.all_violations(rule, generators)
        ess_violations = (
            validation.all_violations(ess_shave, generators)
            if ess_shave is not None and ess_shave.status == "optimal" else []
        )
        reduction = cost_reduction_pct(rule, optimized)

        rows.append({
            "start": str(chunk.index[0]),
            "status": optimized.status,
            "baseline_cost_krw": rule.total_cost_krw,
            "milp_cost_krw": optimized.total_cost_krw,
            "cost_reduction_pct": reduction,
            "build_time_s": optimized.size.build_time_s if optimized.size else None,
            "solve_time_s": optimized.size.solve_time_s if optimized.size else None,
            "wall_time_s": wall_s,
            "milp_violations": [str(v) for v in milp_violations],
            "baseline_violations": [str(v) for v in rule_violations],
            "ess_violations": [str(v) for v in ess_violations],
            "baseline_surplus_mwh": sum(rule.surplus_mw),
            "baseline_shed_mwh": sum(rule.shed_mw),
            "milp_carbon_ton": carbon.total_ton(optimized.units),
            "baseline_carbon_ton": carbon.total_ton(rule.units),
            "peak_net_demand_mw": max(inp.net_demand_mw),
            "min_reserve_pct": min(
                optimized.reserve_pct(t) for t in range(optimized.horizon_h)
            ) if optimized.status == "optimal" else None,
            # ESS
            "ess_cost_status": ess_cost.status if ess_cost else None,
            "ess_cost_reduction_pct": (
                cost_reduction_pct(rule, ess_cost)
                if ess_cost is not None and ess_cost.status == "optimal" else None
            ),
            "ess_cost_peak_reduction_pct": (
                ess_cost.peak_reduction_pct
                if ess_cost is not None and ess_cost.status == "optimal" else None
            ),
            "ess_cost_carbon_ton": (
                carbon.total_ton(ess_cost.units) if ess_cost is not None else None
            ),
            "ess_shave_status": ess_shave.status if ess_shave else None,
            "ess_shave_peak_reduction_pct": (
                ess_shave.peak_reduction_pct
                if ess_shave is not None and ess_shave.status == "optimal" else None
            ),
            "ess_shave_cost_reduction_pct": (
                cost_reduction_pct(rule, ess_shave)
                if ess_shave is not None and ess_shave.status == "optimal" else None
            ),
            "ess_shave_cycles": (
                ess_shave.ess.cycles
                if ess_shave is not None and ess_shave.ess is not None else None
            ),
        })

        def pct(value) -> str:
            return "     -" if value is None else f"{value:6.2f}"

        row = rows[-1]
        print(f"{str(chunk.index[0]):<22}"
              f"{rule.total_cost_krw / 1e8:>11,.0f}"
              f"{optimized.total_cost_krw / 1e8:>10,.0f}"
              f"{reduction:>7.2f}"
              f"{pct(row['ess_cost_reduction_pct']):>10}"
              f"{pct(row['ess_cost_peak_reduction_pct']):>10}"
              f"{pct(row['ess_shave_peak_reduction_pct']):>11}"
              f"{row['solve_time_s']:>9.2f}"
              f"{len(milp_violations) + len(ess_violations):>5}")

    return summarize(rows)


def summarize(rows: list[dict]) -> dict:
    """창별 결과를 KPI 판정으로 접는다."""
    optimal = [r for r in rows if r["status"] == "optimal"]
    reductions = [r["cost_reduction_pct"] for r in optimal]
    runtimes = [r["wall_time_s"] for r in optimal]
    violations = sum(len(r["milp_violations"]) for r in optimal)
    baseline_violations = sum(len(r["baseline_violations"]) for r in optimal)
    ess_violations = sum(len(r["ess_violations"]) for r in rows)

    def collect(key: str) -> list[float]:
        return [r[key] for r in rows if r.get(key) is not None]

    shave_peaks = collect("ess_shave_peak_reduction_pct")
    cost_peaks = collect("ess_cost_peak_reduction_pct")
    ess_cost_reductions = collect("ess_cost_reduction_pct")
    shave_feasible = [r for r in rows if r.get("ess_shave_status") == "optimal"]

    summary = {
        "n_windows": len(rows),
        "n_optimal": len(optimal),
        "cost_reduction_pct": {
            "mean": statistics.fmean(reductions) if reductions else 0.0,
            "min": min(reductions) if reductions else 0.0,
            "max": max(reductions) if reductions else 0.0,
            "target_pct": COST_REDUCTION_TARGET_PCT,
            "target_met": bool(reductions)
            and statistics.fmean(reductions) >= COST_REDUCTION_TARGET_PCT,
        },
        "cost_reduction_with_ess_pct": {
            "mean": statistics.fmean(ess_cost_reductions) if ess_cost_reductions else None,
            "max": max(ess_cost_reductions) if ess_cost_reductions else None,
        },
        "runtime_s": {
            "mean": statistics.fmean(runtimes) if runtimes else 0.0,
            "max": max(runtimes) if runtimes else 0.0,
            "target_s": RUNTIME_TARGET_S,
            "target_met": bool(runtimes) and max(runtimes) <= RUNTIME_TARGET_S,
        },
        "peak_reduction_pct": {
            "cost_mode_mean": statistics.fmean(cost_peaks) if cost_peaks else None,
            "cost_mode_met_windows": sum(
                1 for x in cost_peaks if x >= PEAK_REDUCTION_TARGET_PCT
            ),
            "shave_mode_mean": statistics.fmean(shave_peaks) if shave_peaks else None,
            "shave_mode_min": min(shave_peaks) if shave_peaks else None,
            "shave_feasible_windows": len(shave_feasible),
            "target_pct": PEAK_REDUCTION_TARGET_PCT,
            "target_met": bool(shave_peaks)
            and len(shave_feasible) == len(rows)
            and min(shave_peaks) >= PEAK_REDUCTION_TARGET_PCT - 1e-6,
        },
        "milp_violations": violations,
        "baseline_violations": baseline_violations,
        "ess_violations": ess_violations,
        "windows": rows,
    }

    print("\n── KPI ──")
    red = summary["cost_reduction_pct"]
    run = summary["runtime_s"]
    peak = summary["peak_reduction_pct"]
    print(f"비용 절감률  평균 {red['mean']:.2f}% (최소 {red['min']:.2f} / "
          f"최대 {red['max']:.2f}) · 기준 {red['target_pct']}% "
          f"-> {'통과' if red['target_met'] else '미달'}")
    with_ess = summary["cost_reduction_with_ess_pct"]["mean"]
    if with_ess is not None:
        print(f"  (참고) ESS 포함 MILP vs ESS 없는 baseline: 평균 {with_ess:.2f}% "
              "— 공정성은 [미정], 팀 확정 전까지 참고값")
    print(f"응답시간     평균 {run['mean']:.2f}s · 최대 {run['max']:.2f}s · "
          f"기준 {run['target_s']}s -> {'통과' if run['target_met'] else '미달'}")
    if peak["shave_mode_mean"] is not None:
        print(f"ESS 피크 감소 peak_shaving 평균 {peak['shave_mode_mean']:.2f}% "
              f"(최소 {peak['shave_mode_min']:.2f}, 해 있는 창 "
              f"{peak['shave_feasible_windows']}/{summary['n_windows']}) · "
              f"기준 {peak['target_pct']}% -> {'통과' if peak['target_met'] else '미달'}")
        print(f"             cost 모드 평균 {peak['cost_mode_mean']:.2f}% "
              f"(5% 이상 달성 {peak['cost_mode_met_windows']}창 — 보장되지 않음)")
    print(f"MILP 제약 위반     {violations}건 -> {'통과' if violations == 0 else '실패'}")
    print(f"ESS 제약 위반      {ess_violations}건 -> "
          f"{'통과' if ess_violations == 0 else '실패'}")
    print(f"baseline 제약 위반 {baseline_violations}건 "
          f"(위반이 있으면 비교가 공정하지 않다)")
    print(f"최적해 {summary['n_optimal']}/{summary['n_windows']} 창")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="dispatch 최적화 KPI 측정")
    parser.add_argument("--windows", type=int, default=7, help="측정할 24h 창 수")
    parser.add_argument("--horizon", type=int, default=24, help="창 길이(시간)")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--csv-dir", default="data/csv")
    parser.add_argument("--json", help="결과를 JSON 으로 저장할 경로")
    parser.add_argument("--no-ess", action="store_true", help="ESS 측정을 건너뛴다")
    parser.add_argument("--align-hour", type=int, default=None,
                        help="이 시각에 시작하는 창만 쓴다 (0 = 자정 정렬, day-ahead 기준)")
    parser.add_argument("--peak-target", type=float, default=PEAK_REDUCTION_TARGET_PCT,
                        help="peak_shaving 모드 목표 감소율 (기본 5%%)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s"
    )

    summary = measure(args)
    if args.json:
        Path(args.json).write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nJSON 저장: {args.json}")

    ok = (
        summary["runtime_s"]["target_met"]
        and summary["milp_violations"] == 0
        and summary["ess_violations"] == 0
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
