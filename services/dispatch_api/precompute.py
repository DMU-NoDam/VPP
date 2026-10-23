"""대시보드 VSS 타일용 Two-Stage 미리 계산.

대시보드는 응답을 12초만 기다리는데, RP·EV·EEV 를 시간 제한에 걸리지 않게 풀면 1분 넘게
걸린다. 그래서 서버가 켜질 때와 refresh_hours 마다 백그라운드 스레드가 한 번 풀어 두고,
GET /api/v1/dispatch/stochastic/latest 가 마지막 결과를 돌려준다.

조건은 config/stochastic.yaml 의 precompute 블록이다. 환경변수 PRECOMPUTE_ENABLED=false 면
스레드를 띄우지 않는다 (테스트·로컬 디버깅용).
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import stochastic
from scenarios import generate
from shared.schemas import ScenarioRequest, SolverOptions, StochasticLatest, StochasticResponse
from units import find_file, load_units

logger = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class PrecomputeConfig:
    """config/stochastic.yaml 의 precompute 블록."""

    enabled: bool = True
    seed: int = 1
    n_scenarios: int = 10
    reserve_ratio: float = 0.10
    shed_penalty_won_per_mwh: float = 10_000_000.0
    time_limit_s: float = 60.0
    mip_gap: float = 0.001
    refresh_hours: float = 24.0


def load_config(path: Path | None = None) -> PrecomputeConfig:
    """precompute 블록을 읽는다. 블록이 없으면 기본값."""
    path = path or find_file("STOCHASTIC_YAML", "config", "stochastic.yaml")
    raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("precompute") or {}
    known = {f.name for f in fields(PrecomputeConfig)}
    cfg = PrecomputeConfig(**{k: v for k, v in raw.items() if k in known})
    if os.getenv("PRECOMPUTE_ENABLED", "").lower() == "false":
        cfg = PrecomputeConfig(**{**cfg.__dict__, "enabled": False})
    return cfg


class LatestStore:
    """마지막 계산 결과. 백그라운드 스레드가 쓰고 API 가 읽는다."""

    def __init__(self, cfg: PrecomputeConfig) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._state = "pending" if cfg.enabled else "disabled"
        self._computed_at: datetime | None = None
        self._result: StochasticResponse | None = None
        self._error: str | None = None

    def set_running(self) -> None:
        with self._lock:
            self._state = "running"

    def set_done(self, result: StochasticResponse, at: datetime) -> None:
        with self._lock:
            self._state, self._result, self._computed_at, self._error = "done", result, at, None

    def set_error(self, message: str) -> None:
        # 이전 결과는 남겨 둔다 (화면이 마지막 성공 값을 계속 보여 줄 수 있게)
        with self._lock:
            self._state, self._error = "error", message

    def snapshot(self) -> StochasticLatest:
        with self._lock:
            c = self.cfg
            return StochasticLatest(
                state=self._state, computed_at=self._computed_at, seed=c.seed,
                n_scenarios=c.n_scenarios, reserve_ratio=c.reserve_ratio,
                shed_penalty_won_per_mwh=c.shed_penalty_won_per_mwh,
                time_limit_s=c.time_limit_s, mip_gap=c.mip_gap,
                result=self._result, error=self._error,
            )


def compute_once(store: LatestStore, now: datetime | None = None) -> None:
    """설정대로 시나리오를 만들고 RP·EV·EEV 를 풀어 store 에 넣는다. 실패는 store 에 기록."""
    cfg = store.cfg
    now = now or datetime.now(KST)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    store.set_running()
    try:
        scenarios = generate(ScenarioRequest(n_scenarios=cfg.n_scenarios, seed=cfg.seed)).scenarios
        result = stochastic.run(
            load_units(), scenarios, stochastic.load_params(), start=start,
            reserve_ratio=cfg.reserve_ratio, shed_penalty=cfg.shed_penalty_won_per_mwh,
            opts=SolverOptions(time_limit_s=cfg.time_limit_s, mip_gap=cfg.mip_gap),
            compute_ws=False,
        )
    except Exception as e:  # 스레드가 죽지 않게 모든 실패를 상태로 남긴다
        logger.exception("VSS 미리 계산 실패")
        store.set_error(f"{type(e).__name__}: {e}")
        return
    store.set_done(result, datetime.now(KST))
    logger.info("VSS 미리 계산 완료: %.2f%% (%s, %.0fs)",
                result.vss.vss_pct, result.status, result.solve_time_s)


def start_background(store: LatestStore, stop: threading.Event) -> threading.Thread | None:
    """refresh_hours 마다 compute_once 를 도는 데몬 스레드. 꺼져 있으면 None."""
    if not store.cfg.enabled:
        return None

    def loop() -> None:
        while not stop.is_set():
            compute_once(store)
            stop.wait(store.cfg.refresh_hours * 3600)

    thread = threading.Thread(target=loop, name="vss-precompute", daemon=True)
    thread.start()
    return thread
