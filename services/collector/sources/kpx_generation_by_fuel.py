"""generation_by_fuel ← getPwrAmountByGen 의 fuelPwr1~9 를 세로형 9행으로."""

from __future__ import annotations

from datetime import datetime

import domain

from . import common, kpx_pwr_amount


def fetch(start: datetime, end: datetime) -> list[dict]:
    rows = []
    for ts, item in kpx_pwr_amount.rows_between(start, end):
        ts_text = common.fmt_ts(ts)
        for api_field, fuel in domain.FUEL_BY_API_FIELD.items():
            mw = common.num(str(item.get(api_field, "")))
            if mw is None:
                continue  # 양수(fuelPwr5)는 음수가 정상이라 값 자체는 거르지 않는다
            rows.append({"ts": ts_text, "fuel": fuel, "mw": mw})
    return rows
