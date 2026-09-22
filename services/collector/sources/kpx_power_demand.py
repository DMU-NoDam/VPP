"""power_demand ← getPwrAmountByGen 의 fuelPwrTot (합계 = 현재 시장수요)."""

from __future__ import annotations

from datetime import datetime

from . import common, kpx_pwr_amount


def fetch(start: datetime, end: datetime) -> list[dict]:
    rows = []
    for ts, item in kpx_pwr_amount.rows_between(start, end):
        demand = common.num(str(item.get("fuelPwrTot", "")))
        if demand is None:
            continue
        rows.append({"ts": common.fmt_ts(ts), "demand_mw": demand})
    return rows
