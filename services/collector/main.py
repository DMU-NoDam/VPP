"""collector 스텁 — 공공데이터 수집기 자리만 잡아둔 상태."""

import logging
import time

INTERVAL_SEC = 3600

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("collector")


def main() -> None:
    while True:
        logger.info("collector stub — 공공데이터 수집 예정")
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
