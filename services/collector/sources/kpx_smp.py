"""smp ← getSmpWithForecastDemand (육지 행만).

주의할 점 두 가지.
- hour 가 "01" 과 "1" 로 섞여 와서 같은 시각이 두 번 나온다. int 로 바꿔 중복을 없앤다.
- hour 는 종료 시각 기준이라 "01" 이 00:00~01:00 이다. 저장은 시작 시각으로 맞춘다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import common

URL = "https://apis.data.go.kr/B552115/SmpWithForecastDemand/getSmpWithForecastDemand"
ROWS_PER_PAGE = 200  # 48 로 잡으면 중복 표기 탓에 23·24시가 잘린다
AREA = "육지"


def fetch(start: datetime, end: datetime) -> list[dict]:
    by_ts: dict[str, dict] = {}

    for day in common.days(start, end):
        payload = common.get_json(URL, {
            "serviceKey": common.service_key(),
            "dataType": "json",
            "pageNo": 1,
            "numOfRows": ROWS_PER_PAGE,
            "date": day.strftime("%Y%m%d"),
        })
        common.check_result(payload, URL)

        for item in common.items(payload):
            if item.get("areaName") != AREA:
                continue

            hour = _parse_hour(item.get("hour"))
            smp = common.num(str(item.get("smp", "")))
            if hour is None or smp is None:
                continue

            ts = datetime(day.year, day.month, day.day) + timedelta(hours=hour - 1)
            if not (start <= ts <= end):
                continue

            by_ts[common.fmt_ts(ts)] = {"ts": common.fmt_ts(ts), "smp_won_per_kwh": smp}

    return list(by_ts.values())


def _parse_hour(value) -> int | None:
    try:
        hour = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return hour if 1 <= hour <= 24 else None
