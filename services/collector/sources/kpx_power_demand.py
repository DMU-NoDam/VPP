"""power_demand ← KPX 전력수급현황 화면 (sukub.do) 의 현재수요.

공식 API 가 아니라 HTML 화면이다. 표의 <tr> 하나가 한 시각이고 <td> 가 8칸이다.
주의할 점 세 가지.
- 칸마다 값 뒤에 &nbsp; 가 붙어 온다. 떼고 읽는다.
- 한 페이지가 17행뿐이고 하루는 288행(17페이지)이다. 하루 단위로 부르되, 첫 날은 start 가
  들어있을 페이지부터 읽어서 주기 수집 때 앞 페이지를 매번 다시 받지 않게 한다.
- 범위를 넘긴 페이지도 200 이고 데이터 행만 0개다. 행이 없으면 그 날은 끝이다.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from . import common

URL = "https://openapi.kpx.or.kr/sukub.do"
ROWS_PER_PAGE = 17
MAX_PAGES = 20  # 하루 17페이지
STEP_MINUTES = 5
API_TS_FORMAT = "%Y%m%d%H%M%S"
I_TS, I_DEMAND = 0, 2  # 기준일시, 현재수요(MW)
CELLS_PER_ROW = 8

_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)


def fetch(start: datetime, end: datetime) -> list[dict]:
    rows: list[dict] = []
    for day in common.days(start, end):
        first_page = _page_of(start) if day == start.date() else 1
        for ts, demand in _fetch_day(day, first_page, start, end):
            if start <= ts <= end:
                rows.append({"ts": common.fmt_ts(ts), "demand_mw": demand})
    return rows


def _fetch_day(day: date, first_page: int, start: datetime,
               end: datetime) -> list[tuple[datetime, float | None]]:
    picked: list[tuple[datetime, float | None]] = []

    # 원본에 빠진 시각이 있으면 행이 앞으로 당겨져서 계산한 페이지가 start 를 지나쳐 있다.
    # 그 페이지의 첫 시각이 start 보다 뒤면 한 페이지씩 물러난다.
    page = first_page
    parsed = _fetch_page(day, page)
    while page > 1 and (not parsed or parsed[0][0] > start):
        page -= 1
        parsed = _fetch_page(day, page)

    while parsed:
        picked.extend(parsed)
        if len(parsed) < ROWS_PER_PAGE or page >= MAX_PAGES:
            break
        if parsed[-1][0] >= end:
            break  # 시간순이라 end 를 지났으면 뒤 페이지는 볼 필요 없다
        page += 1
        parsed = _fetch_page(day, page)

    return picked


def _fetch_page(day: date, page: int) -> list[tuple[datetime, float | None]]:
    text = common.get_text(URL, {
        "startDate": day.isoformat(),
        "endDate": day.isoformat(),
        "pageIndex": page,
    })
    return _parse(text)


def _page_of(ts: datetime) -> int:
    """그 날 00:00 부터 5분 간격으로 빠짐없이 있다고 칠 때 ts 가 놓이는 페이지."""
    index = (ts.hour * 60 + ts.minute) // STEP_MINUTES
    return index // ROWS_PER_PAGE + 1


def _parse(text: str) -> list[tuple[datetime, float | None]]:
    rows = []
    for tr in _TR.findall(text):
        cells = [c.replace("&nbsp;", "").strip() for c in _TD.findall(tr)]
        if len(cells) != CELLS_PER_ROW:
            continue

        try:
            ts = datetime.strptime(cells[I_TS], API_TS_FORMAT)
        except ValueError:
            continue  # 첫 칸이 14자리 시각이 아니면 데이터 행이 아니다

        rows.append((ts, common.num(cells[I_DEMAND])))  # 값이 비어 있으면 None 으로 둔다
    return rows
