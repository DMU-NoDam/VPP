"""dam_status ← K-water 수문 운영 정보 (sluicePresentCondition/mntlist).

댐은 DAM_CODES (쉼표 구분, 기본 1012110 소양강). 하루 단위(stdt/eddt)로 부른다.
응답의 obsrdtmnt 는 "09-18 00시 10분" 꼴이라 연도가 없다. 요청한 날짜의 연도를 붙인다.
숫자는 "1,991.13" 처럼 쉼표가 섞여 오고 타입도 문자열/숫자가 뒤섞여서 여기서 정리한다.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta

from . import common

URL = "https://apis.data.go.kr/B500001/dam/sluicePresentCondition/mntlist"
DAM_CODES = [c.strip() for c in os.getenv("DAM_CODES", "1012110").split(",") if c.strip()]
ROWS_PER_PAGE = 500
MAX_PAGES = 10

_OBSRD = re.compile(r"(\d{1,2})-(\d{1,2})\s*(\d{1,2})\s*시\s*(\d{1,2})\s*분")


def fetch(start: datetime, end: datetime) -> list[dict]:
    rows: list[dict] = []
    for code in DAM_CODES:
        for day in common.days(start, end):
            rows.extend(_fetch_day(code, day, start, end))
    return rows


def _fetch_day(code: str, day: date, start: datetime, end: datetime) -> list[dict]:
    rows: list[dict] = []

    for page in range(1, MAX_PAGES + 1):
        payload = common.get_json(URL, {
            "serviceKey": common.service_key(),
            "damcode": code,
            "stdt": day.isoformat(),
            "eddt": day.isoformat(),
            "pageNo": page,
            "numOfRows": ROWS_PER_PAGE,
            "_type": "json",
        })
        common.check_result(payload, URL)

        items = common.items(payload)
        if not items:
            break

        for item in items:
            observed_at = _parse_observed_at(item.get("obsrdtmnt"), day)
            if observed_at is None or not (start <= observed_at <= end):
                continue
            rows.append({
                "observed_at": common.fmt_ts(observed_at),
                "dam_code": code,
                "water_level_m": common.num(str(item.get("lowlevel", ""))),
                "inflow_cms": common.num(str(item.get("inflowqy", ""))),
                "total_discharge_cms": common.num(str(item.get("totdcwtrqy", ""))),
                "storage_mcm": common.num(str(item.get("rsvwtqy", ""))),
                "storage_rate_pct": common.num(str(item.get("rsvwtrt", ""))),
            })

        if page * ROWS_PER_PAGE >= common.total_count(payload):
            break

    return rows


def _parse_observed_at(value, day: date) -> datetime | None:
    """"09-18 00시 10분" + 요청 날짜의 연도 -> datetime."""
    m = _OBSRD.search(str(value or ""))
    if not m:
        return None
    month, dom, hour, minute = (int(g) for g in m.groups())
    try:
        if hour == 24:  # 24시 표기는 다음 날 00시다
            return datetime(day.year, month, dom, 0, minute) + timedelta(days=1)
        return datetime(day.year, month, dom, hour, minute)
    except ValueError:
        return None
