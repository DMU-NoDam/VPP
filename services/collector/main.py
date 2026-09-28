"""collector 엔트리포인트.

컨테이너는 하나이고, 이 파일이 store / collector / data_api 를 조립해 실행한다.
데이터 정의는 domain.py, 데이터셋↔API 매칭은 collector.py 가 갖는다.
DB 는 쓰지 않는다. 데이터는 data/csv 의 CSV 로 보관하고 기동 시 메모리에 올린다.

기동 순서 (자세한 건 startup.md)
  1. CSV 읽어 메모리 적재      store.load
  2. 결측 구간 탐지            Tables.find_gaps
  3. 결측을 API 로 채움        collector.call_api -> Tables.add
  4. CSV 에 append             store.save
  5. data_api 기동             data_api.create_app
  6. 주기적으로 2~4 를 다시 돌기 (실시간 수집 = 구간이 짧은 백필)

1~4 는 동기다. 다 끝난 뒤에 5, 6 이 올라간다.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from pathlib import Path

import uvicorn

import collector
import data_api
import domain
import store

# --- 설정 -------------------------------------------------------------------

CSV_DIR = Path(os.getenv("CSV_DIR", "data/csv"))
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "300"))  # 수집 주기, 원본이 5분 간격
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("collector.main")


# --- 1~4단계: 적재 -> 결측 탐지 -> 백필 -> 기록 ------------------------------


def bootstrap() -> domain.Tables:
    """CSV 를 메모리에 올리고, 마지막 시각부터 지금까지를 API 로 채운다.

    동기다. 이게 끝나야 data_api 가 뜬다.
    """
    tables = store.load(CSV_DIR)
    logger.info("CSV 적재 완료: %s", tables.summary())

    collect(tables, datetime.now())
    logger.info("백필 완료: %s", tables.summary())
    return tables


def collect(tables: domain.Tables, now: datetime) -> None:
    """2~4단계: 결측을 찾아 API 로 채우고 CSV 에 적는다."""
    gaps = tables.find_gaps(now)
    if not gaps:
        logger.info("결측 없음")
        return

    for gap in gaps:
        _fill(tables, gap)

    store.save(CSV_DIR, tables)


def _fill(tables: domain.Tables, gap: domain.Gap) -> None:
    """구간 하나를 받아 메모리에 반영한다. 실패해도 예외를 올리지 않는다."""
    try:
        rows = collector.call_api(gap.dataset, gap.start, gap.end)
    except Exception:
        # 한 데이터셋이 실패해도 나머지는 진행한다. 다음 틱에 다시 결측으로 잡힌다.
        logger.exception("%s 수집 실패 (%s ~ %s)", gap.dataset, gap.start, gap.end)
        return

    added = tables.add(gap.dataset, rows)
    logger.info("%s %d행 수신, %d행 추가", gap.dataset, len(rows), added)


# --- 6단계: 주기 수집 루프 ---------------------------------------------------


def collect_loop(tables: domain.Tables, stop: threading.Event) -> None:
    """INTERVAL_SEC 마다 2~4단계를 현재시각으로 다시 돈다."""
    while not stop.wait(INTERVAL_SEC):  # 주기만큼 자다가 stop 이 서면 바로 깬다
        collect(tables, datetime.now())


# --- 5단계: data_api ---------------------------------------------------------


def serve_api(tables: domain.Tables, stop: threading.Event) -> None:
    """uvicorn 을 이 스레드에서 돌린다. 종료되면 stop 을 세워 수집 루프도 끝낸다."""
    app = data_api.create_app(tables)
    config = uvicorn.Config(app, host=API_HOST, port=API_PORT, log_level="info")
    try:
        uvicorn.Server(config).run()
    finally:
        stop.set()


# --- 엔트리 ------------------------------------------------------------------


def run() -> None:
    tables = bootstrap()  # 1~4단계. 여기가 끝나야 아래로 넘어간다

    stop = threading.Event()
    worker = threading.Thread(target=collect_loop, args=(tables, stop), daemon=True)
    worker.start()  # 6단계. 첫 동작이 INTERVAL_SEC 대기라 실제 수집은 서버가 뜬 뒤다

    logger.info("data_api %s:%d, 수집 주기 %ds", API_HOST, API_PORT, INTERVAL_SEC)
    try:
        serve_api(tables, stop)  # 5단계. 메인 스레드에서 돌아야 종료 신호를 받는다
    finally:
        stop.set()
        worker.join(timeout=10)
        logger.info("종료")


def main() -> None:
    run()


if __name__ == "__main__":
    main()
