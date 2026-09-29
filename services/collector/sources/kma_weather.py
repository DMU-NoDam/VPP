"""weather ← 기상청 API허브 kma_sfctm3 (고정폭 텍스트).

지점은 KMA_STATION_IDS (쉼표 구분, 기본 108 서울). 한 번에 길게 요청하면 504 가 나서
CHUNK_DAYS 단위로 잘라 부른다.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from . import common

URL = "https://apihub.kma.go.kr/api/typ01/url/kma_sfctm3.php"
STATION_IDS = [s.strip() for s in os.getenv("KMA_STATION_IDS", "108").split(",") if s.strip()]
CHUNK_DAYS = int(os.getenv("KMA_CHUNK_DAYS", "10"))
TM_FORMAT = "%Y%m%d%H%M"

# 앞쪽 컬럼은 위치가 고정이다.
I_TM, I_STN, I_WS, I_TA, I_HM = 0, 1, 3, 11, 13
# SI 는 WW·CT 같은 가변폭 문자열 뒤라 앞에서 세면 밀린다. 뒤에서 센다.
I_SI = -12
MIN_TOKENS = 40


def fetch(start: datetime, end: datetime) -> list[dict]:
    rows: list[dict] = []
    for station in STATION_IDS:
        for chunk_start, chunk_end in _chunks(start, end):
            text = common.get_text(URL, {
                "tm1": chunk_start.strftime(TM_FORMAT),
                "tm2": chunk_end.strftime(TM_FORMAT),
                "stn": station,
                "help": 0,
                "authKey": common.kma_auth_key(),
            })
            rows.extend(_parse(text, start, end))
    return rows


def _chunks(start: datetime, end: datetime):
    span = timedelta(days=CHUNK_DAYS)
    cur = start
    while cur <= end:
        yield cur, min(cur + span, end)
        cur += span + timedelta(minutes=1)


def _parse(text: str, start: datetime, end: datetime) -> list[dict]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split()
        if len(parts) < MIN_TOKENS:
            continue

        try:
            observed_at = datetime.strptime(parts[I_TM], TM_FORMAT)
        except ValueError:
            continue
        if not (start <= observed_at <= end):
            continue

        rows.append({
            "observed_at": common.fmt_ts(observed_at),
            "station_id": parts[I_STN],
            "wind_speed_ms": common.num(parts[I_WS]),
            "temperature_c": common.num(parts[I_TA]),
            "humidity_pct": common.num(parts[I_HM]),
            "solar_radiation_mj": common.num(parts[I_SI]),
        })
    return rows
