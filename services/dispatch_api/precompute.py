"""대시보드 VSS 타일용 Two-Stage 미리 계산.

대시보드는 응답을 12초만 기다리는데, RP·EV·EEV 를 시간 제한 없이 풀면 1분 넘게
걸린다. 그래서 서버가 켜질 때와 refresh_hours 마다 백그라운드 스레드가 한 번 풀어 두고,
GET /api/v1/dispatch/stochastic/latest 가 마지막 결과를 돌려준다.

조건은 config/stochastic.yaml 의 precompute 블록이다. 기준 예측은 forecast_api 에서 받고
(실패하면 base_profile), 계획 시작 시각은 예측 구간의 시작 시각이다.
base_profile 로 폴백한 결과면 retry_interval 마다 forecast_api 를 다시 불러 보고, 응답이 오면
그때 다시 푼다 (dispatch_api 가 forecast_api 보다 먼저 켜진 경우).
환경변수 PRECOMPUTE_ENABLED=false 면 스레드를 띄우지 않는다 (테스트·로컬 디버깅용).
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from forecast_client import KST, resolve_base

import stochastic
from scenarios import generate
from shared.schemas import ScenarioRequest, SolverOptions, StochasticLatest, StochasticResponse
from units import cfg_value, load_units, read_stochastic_yaml

logger = logging.getLogger(__name__)
SECONDS_PER_HOUR = 3600
SECONDS_PER_MINUTE = 60
FALLBACK_SOURCE = "config"  # resolve_base 가 base_profile 로 폴백했을 때의 ScenarioProfile.source


def today_start(now: datetime | None = None) -> datetime:
    """오늘 0시 (KST)."""
    now = now or datetime.now(KST)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class PrecomputeConfig:
    """config/stochastic.yaml 의 precompute 블록."""

    enabled: bool
    seed: int
    n_scenarios: int
    reserve_ratio: float
    reserve_mw: float
    shed_penalty_won_per_mwh: float
    time_limit_s: float | None  # None 이면 제한 없음
    mip_gap: float
    refresh_hours: float
    retry_minutes: float  # base_profile 폴백 결과일 때 forecast_api 를 다시 불러 보는 간격


def load_config(path: Path | None = None) -> PrecomputeConfig:
    """precompute 블록을 읽는다. 환경변수 PRECOMPUTE_ENABLED=false 면 꺼 둔다."""
    raw = read_stochastic_yaml(path)["precompute"]
    enabled = bool(raw["enabled"]) and os.getenv("PRECOMPUTE_ENABLED", "").lower() != "false"
    time_limit = cfg_value(raw["time_limit"], "s")
    return PrecomputeConfig(
        enabled=enabled,
        seed=int(cfg_value(raw["seed"], "-")),
        n_scenarios=int(cfg_value(raw["n_scenarios"], "count")),
        reserve_ratio=float(cfg_value(raw["reserve_ratio"], "ratio")),
        reserve_mw=float(cfg_value(raw["reserve_mw"], "MW")),
        shed_penalty_won_per_mwh=float(cfg_value(raw["shed_penalty"], "KRW/MWh")),
        time_limit_s=None if time_limit is None else float(time_limit),
        mip_gap=float(cfg_value(raw["mip_gap"], "ratio")),
        refresh_hours=float(cfg_value(raw["refresh_interval"], "h")),
        retry_minutes=float(cfg_value(raw["retry_interval"], "min")),
    )


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

    def used_fallback_base(self) -> bool:
        """마지막 결과가 forecast_api 예측이 아니라 base_profile 로 계산됐는지."""
        with self._lock:
            return self._result is not None and self._result.base_source == FALLBACK_SOURCE

    def snapshot(self) -> StochasticLatest:
        with self._lock:
            c = self.cfg
            return StochasticLatest(
                state=self._state, computed_at=self._computed_at, seed=c.seed,
                n_scenarios=c.n_scenarios, reserve_ratio=c.reserve_ratio,
                reserve_mw=c.reserve_mw,
                shed_penalty_won_per_mwh=c.shed_penalty_won_per_mwh,
                time_limit_s=c.time_limit_s, mip_gap=c.mip_gap,
                result=self._result, error=self._error,
            )


def compute_once(
    store: LatestStore, now: datetime | None = None, *, forecast_only: bool = False
) -> None:
    """설정대로 시나리오를 만들고 RP·EV·EEV 를 풀어 store 에 넣는다. 실패는 store 에 기록.

    forecast_only 면 기준 예측이 base_profile 로 폴백했을 때 풀지 않고 이전 결과를 그대로 둔다.
    """
    cfg = store.cfg
    now = now or datetime.now(KST)
    try:
        base = resolve_base(now)
        if forecast_only and base.source == FALLBACK_SOURCE:
            logger.info("forecast_api 기준 예측이 아직 없어 VSS 를 다시 계산하지 않는다")
            return
        store.set_running()
        start = base.start or today_start(now)
        scenarios = generate(
            ScenarioRequest(n_scenarios=cfg.n_scenarios, seed=cfg.seed, base=base)
        ).scenarios
        result = stochastic.run(
            load_units(), scenarios, stochastic.load_params(), start=start,
            reserve_ratio=cfg.reserve_ratio, reserve_mw=cfg.reserve_mw,
            shed_penalty=cfg.shed_penalty_won_per_mwh,
            opts=SolverOptions(time_limit_s=cfg.time_limit_s, mip_gap=cfg.mip_gap),
            compute_ws=False,
        ).model_copy(update={"base_source": base.source})
    except Exception as e:  # 스레드가 죽지 않게 모든 실패를 상태로 남긴다
        logger.exception("VSS 미리 계산 실패")
        store.set_error(f"{type(e).__name__}: {e}")
        return
    store.set_done(result, datetime.now(KST))
    logger.info("VSS 미리 계산 완료: %.2f%% (%s, %.0fs)",
                result.vss.vss_pct, result.status, result.solve_time_s)


def next_wait_s(store: LatestStore) -> float:
    """다음 계산까지 기다릴 시간. base_profile 폴백 결과면 retry_minutes, 아니면 refresh_hours."""
    if store.used_fallback_base():
        return store.cfg.retry_minutes * SECONDS_PER_MINUTE
    return store.cfg.refresh_hours * SECONDS_PER_HOUR


def start_background(store: LatestStore, stop: threading.Event) -> threading.Thread | None:
    """refresh_hours 마다 compute_once 를 도는 데몬 스레드. 꺼져 있으면 None."""
    if not store.cfg.enabled:
        return None

    def loop() -> None:
        while not stop.is_set():
            # 직전 결과가 base_profile 폴백이면 forecast_api 가 응답할 때만 다시 푼다
            compute_once(store, forecast_only=store.used_fallback_base())
            stop.wait(next_wait_s(store))

    thread = threading.Thread(target=loop, name="vss-precompute", daemon=True)
    thread.start()
    return thread
