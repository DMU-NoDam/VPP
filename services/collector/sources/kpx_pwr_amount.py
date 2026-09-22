"""getPwrAmountByGen (발전원별 발전량, 계통기준) 원응답 조회.

power_demand 와 generation_by_fuel 두 source 가 이걸 쓴다. 데이터셋마다 한 번씩
부르므로 같은 구간을 두 번 받아오게 된다 (알고 있음, 나중에 수정).

baseDate 파라미터가 무시되고 전량이 최신순으로 내려오는 API 라, 페이지를 돌면서
원하는 구간만 걸러낸다.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from . import common

logger = logging.getLogger(__name__)

URL = "https://apis.data.go.kr/B552115/PwrAmountByGen/getPwrAmountByGen"
# 페이지를 크게 잡으면 응답이 안 온다 (300행 4초, 1000행은 90초에도 무응답).
ROWS_PER_PAGE = int(os.getenv("PWR_AMOUNT_ROWS", "300"))
MAX_PAGES = int(os.getenv("PWR_AMOUNT_MAX_PAGES", "50"))
API_TS_FORMAT = "%Y%m%d%H%M%S"


def rows_between(start: datetime, end: datetime) -> list[tuple[datetime, dict]]:
    """[start, end] 안에 드는 (시각, 원응답 행) 목록. 최신순으로 받아 구간만 남긴다."""
    picked: list[tuple[datetime, dict]] = []

    for page in range(1, MAX_PAGES + 1):
        payload = common.get_json(URL, {
            "serviceKey": common.service_key(),
            "dataType": "json",
            "pageNo": page,
            "numOfRows": ROWS_PER_PAGE,
            "baseDate": end.strftime("%Y%m%d"),  # 무시되지만 명세상 필수
        })
        common.check_result(payload, URL)

        items = common.items(payload)
        if not items:
            break

        oldest: datetime | None = None
        for item in items:
            ts = _parse_ts(item.get("baseDatetime"))
            if ts is None:
                continue
            if oldest is None or ts < oldest:
                oldest = ts
            if start <= ts <= end:
                picked.append((ts, item))

        # 최신순이므로 페이지의 가장 오래된 시각이 start 를 넘어섰으면 더 볼 게 없다.
        if oldest is not None and oldest < start:
            break
        if len(items) < ROWS_PER_PAGE:
            break
    else:
        logger.warning("getPwrAmountByGen 페이지 상한(%d) 도달 — %s 이전은 못 받았다",
                       MAX_PAGES, start)

    return picked


def _parse_ts(value) -> datetime | None:
    try:
        return datetime.strptime(str(value), API_TS_FORMAT)
    except (TypeError, ValueError):
        return None
