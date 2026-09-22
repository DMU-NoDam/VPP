"""데이터셋 이름을 받아 해당 API 를 부른다.

데이터셋↔API 매칭을 이 파일이 갖는다. 행을 만들어 돌려주기만 하고 `Tables` 는 건드리지
않는다. 저장도 하지 않는다.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sources import (
    kma_weather,
    kpx_generation_by_fuel,
    kpx_power_demand,
    kpx_smp,
    kwater_dam,
)

logger = logging.getLogger(__name__)

# 데이터셋 -> API 이름. 로그와 문서용이고, 실제 호출은 아래 source 모듈이 한다.
API_BY_DATASET: dict[str, str] = {
    "power_demand": "getPwrAmountByGen",
    "generation_by_fuel": "getPwrAmountByGen",
    "smp": "getSmpWithForecastDemand",
    "weather": "kma_sfctm3",
    "dam_status": "sluicePresentCondition/mntlist",
}

# 데이터셋 -> source 모듈. 모듈마다 fetch(start, end) 를 갖는다.
# getPwrAmountByGen 이 두 줄에 걸려 있어 한 틱에 같은 API 를 두 번 부른다 (나중에 수정).
SOURCE_BY_DATASET = {
    "power_demand": kpx_power_demand,
    "generation_by_fuel": kpx_generation_by_fuel,
    "smp": kpx_smp,
    "weather": kma_weather,
    "dam_status": kwater_dam,
}


def call_api(dataset: str, start: datetime, end: datetime) -> list[dict]:
    """해당 데이터셋의 [start, end] 구간 행을 받아온다. 없으면 빈 리스트."""
    source = SOURCE_BY_DATASET.get(dataset)
    if source is None:
        logger.debug("%s 는 API 소스가 없다 (CSV 전용)", dataset)
        return []

    logger.info("%s <- %s (%s ~ %s)", dataset, API_BY_DATASET[dataset], start, end)
    return source.fetch(start, end)
