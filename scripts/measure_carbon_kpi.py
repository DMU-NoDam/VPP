"""기존 비용 KPI를 바꾸지 않고 여러 실측 수요 창의 탄소 감축 비용을 측정한다.

실행: .venv/bin/python scripts/measure_carbon_kpi.py --windows 3 --json /tmp/carbon.json
수요는 각 창의 실측, 연료비·설비·저수율은 현재 설정 기준의 재현 시나리오다.
"""

from __future__ import annotations

import argparse
import json

from measure_dispatch_kpi import load_net_demand, windows

import service
from shared.schemas import ParetoRequest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", type=int, default=3)
    parser.add_argument("--points", type=int, default=3)
    parser.add_argument("--time-limit", type=float, default=30)
    parser.add_argument("--targets", type=float, nargs="+", default=[5, 10])
    parser.add_argument("--json", required=True)
    args = parser.parse_args()
    if args.windows < 1:
        parser.error("--windows must be positive")
    cfg = service.settings()
    demand = load_net_demand(cfg, service.CSV_DIR)
    chunks = windows(demand, 24, args.windows, align_hour=0)
    if not chunks:
        parser.error("연속 24시간 데이터가 없습니다")
    rows = []
    for chunk in chunks:
        result = service.run_pareto(ParetoRequest(
            net_demand_mw=chunk.tolist(), start=chunk.index[0].to_pydatetime(),
            points=args.points, total_time_limit_s=args.time_limit,
            reduction_targets_pct=args.targets, ess_mode="cost",
        ))
        rows.append(result.model_dump(mode="json"))
        print(f"{chunk.index[0]}: {result.state}, {result.elapsed_s:.2f}s, "
              f"최대 감축률 {result.maximum_reduction_pct}")
    from pathlib import Path

    Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
