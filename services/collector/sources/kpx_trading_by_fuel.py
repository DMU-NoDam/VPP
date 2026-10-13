"""trading_by_fuel ← getPowerTradingResultInfo1 (전력거래실적, 연료원 × 시간).

주의할 점 네 가지.
- 원본 연료원은 20여 종인데 저장은 domain.FUELS 10종뿐이다. FUEL_BY_NAME 에 없는 연료원은
  버리고, 한 코드에 여러 연료원이 걸리면 (유류, 신재생) 합쳐서 한 행으로 만든다.
- time 은 "00"~"23" 이고 시작 시각 기준이다 ("00" 이 00:00~01:00). 그대로 저장한다.
  과거 CSV 는 같은 구간을 종료 시각 "01"~"24" 로 적으니 헷갈리지 말 것.
- 거래 실적은 늦게 올라온다. 응답에 24시간이 다 들어 있는 날만 올라온 날로 본다.
  그렇지 않은 날을 만나면 거기서 멈추고 그 날부터는 행을 만들지 않는다. None 행으로
  적어두면 나중에 실적이 올라와도 기존 행이 이겨서 값이 안 들어가기 때문이다.
  행이 없으면 다음 틱에 다시 결측으로 잡혀서 올라올 때까지 그 날을 다시 물어본다.
- 올라온 날 안에서 응답에 없는 칸(시각 × 10종)은 None 으로 둔다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import domain

from . import common

URL = "https://apis.data.go.kr/B552115/PowerTradingResultInfo1/getPowerTradingResultInfo1"
ROWS_PER_PAGE = 1000  # 하루가 연료원 × 24시간이라 700행 안팎이다
MAX_PAGES = 5
HOURS_PER_DAY = 24

# 원본 연료원명 -> 코드값. 여기 없는 연료원은 저장하지 않는다.
FUEL_BY_NAME: dict[str, str] = {
    "수력": "hydro",
    "중유": "oil",
    "경유": "oil",
    "유연탄": "bituminous_coal",
    "원자력": "nuclear",
    "양수": "pumped",
    "LNG": "lng",
    "무연탄": "anthracite",
    "태양광": "solar",
    "풍력": "wind",
    # 신재생: 태양광·풍력·수력을 뺀 나머지 신에너지·재생에너지
    "연료전지": "renewable",
    "IGCC": "renewable",         # 과거 CSV 표기
    "IGCC 발전기": "renewable",  # API 표기
    "바이오가스": "renewable",
    "바이오매스": "renewable",
    "바이오중유": "renewable",
    "바이오SRF": "renewable",
    "매립가스": "renewable",
    "폐기물": "renewable",
    "소수력": "renewable",
    "해양에너지": "renewable",
}


def fetch(start: datetime, end: datetime) -> list[dict]:
    entries: list[tuple[datetime, str, float | None, float | None]] = []
    hours: list[datetime] = []

    for day in common.days(start, end):
        day_entries = _day_entries(day)
        if len({ts for ts, *_ in day_entries}) < HOURS_PER_DAY:
            break  # 아직 안 올라온 날. 뒷날도 없다고 보고 멈춘다

        midnight = datetime(day.year, day.month, day.day)
        hours.extend(
            ts for ts in (midnight + timedelta(hours=h) for h in range(HOURS_PER_DAY))
            if start <= ts <= end
        )
        entries.extend(e for e in day_entries if start <= e[0] <= end)

    return to_rows(entries, hours)


def _day_entries(day: date) -> list[tuple[datetime, str, float | None, float | None]]:
    """하루치 응답을 (시작 시각, 원본 연료원명, 설비용량, 거래량) 으로."""
    entries = []
    for item in _fetch_day(day):
        hour = _parse_hour(item.get("time"))
        if hour is None:
            continue
        entries.append((
            datetime(day.year, day.month, day.day) + timedelta(hours=hour),
            str(item.get("fuel", "")),
            common.num(str(item.get("pcap", ""))),
            common.num(str(item.get("mgo", ""))),
        ))
    return entries


def to_rows(entries, hours=()) -> list[dict]:
    """(시각, 원본 연료원명, 설비용량, 거래량) 들을 10종 코드로 묶어 데이터셋 행으로 만든다.

    hours 를 주면 그 시각들은 응답에 없어도 10종 행을 만든다. 값이 없는 칸은 None 이다.
    """
    by_key: dict[tuple[str, str], dict] = {}

    for ts in hours:
        for fuel in domain.FUEL_CODES:
            _row(by_key, common.fmt_ts(ts), fuel)

    for ts, name, capacity_mw, trade_mwh in entries:
        fuel = FUEL_BY_NAME.get(name.strip())
        if fuel is None:
            continue

        row = _row(by_key, common.fmt_ts(ts), fuel)
        row["capacity_mw"] = _add(row["capacity_mw"], capacity_mw)
        row["trade_mwh"] = _add(row["trade_mwh"], trade_mwh)

    return list(by_key.values())


def _row(by_key: dict, ts_text: str, fuel: str) -> dict:
    return by_key.setdefault(
        (ts_text, fuel),
        {"ts": ts_text, "fuel": fuel, "capacity_mw": None, "trade_mwh": None},
    )


def _add(total: float | None, value: float | None) -> float | None:
    """값이 없는 쪽은 건너뛰고 더한다. 둘 다 없으면 None."""
    if value is None:
        return total
    # 원본이 소수 8자리까지 오므로 합산 오차는 그 아래에서 잘라낸다.
    return round((total or 0.0) + value, 6)


def _fetch_day(day: date) -> list[dict]:
    items: list[dict] = []

    for page in range(1, MAX_PAGES + 1):
        payload = common.get_json(URL, {
            "serviceKey": common.service_key(),
            "dataType": "json",
            "pageNo": page,
            "numOfRows": ROWS_PER_PAGE,
            "tradeDay": day.strftime("%Y%m%d"),
        })
        common.check_result(payload, URL)

        page_items = common.items(payload)
        if not page_items:
            break
        items.extend(page_items)

        if page * ROWS_PER_PAGE >= common.total_count(payload):
            break

    return items


def _parse_hour(value) -> int | None:
    try:
        hour = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return hour if 0 <= hour < HOURS_PER_DAY else None
