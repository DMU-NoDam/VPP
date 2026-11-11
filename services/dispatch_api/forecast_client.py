"""forecast_api 연동 — 시나리오 생성의 기준 예측(base)과 90% 예측구간을 REST 로 받는다.

CLAUDE.md 규칙 4: forecast_api 모듈을 import 하지 않고 POST /api/v1/forecast 만 부른다.
부를 수 없거나 응답으로 기준 예측을 만들 수 없으면 config 의 base_profile 로 폴백한다.

Day-ahead 조건 (config/stochastic.yaml 의 forecast 블록):
    계획일 D 의 0시~23시를, 전날 (24 - lead)시까지의 데이터로 예측한다. 즉 lead ~ lead + horizon - 1
    시간 뒤 예측이다 (lead 13 이면 전날 11시 데이터로 13~36시간 뒤). forecast_api 는 "데이터 마지막
    시각 + 1h" 부터 예측하므로 end_date 에 그 결정 시각을 주고 lead + horizon - 1 시간을 받아 뒤의
    horizon 시간만 쓴다.

세 target 의 데이터 끝 시각이 다르다 (수요는 5분 실측, 태양광·풍력은 연료원별 거래량이라 늦게
들어온다). 그래서 먼저 지금 시각으로 한 번 불러 target 마다의 데이터 끝 시각을 알아내고
(= 첫 예측 시각 - 1h), 세 target 모두 데이터가 있는 가장 최근 결정 시각으로 다시 부른다.

end_date 에 시각("YYYY-MM-DD HH:MM:SS")을 넣는 것은 forecast_api 스키마 설명(YYYY-MM-DD)에 없는
사용법이다. 2026-11-03 로컬에서 동작을 확인했고, B 와 계약으로 맞춰야 한다.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scenarios import DEFAULT_BASE
from shared.constants import API_PREFIX
from shared.schemas import ScenarioProfile
from units import cfg_value, read_stochastic_yaml

logger = logging.getLogger(__name__)
# 한국 표준시. 서머타임이 없어 고정 오프셋으로 충분하다 (컨테이너에 tzdata 가 없어도 동작)
KST = timezone(timedelta(hours=9))
TARGETS = ("demand", "solar", "wind")
HOUR = timedelta(hours=1)


class ForecastUnavailableError(RuntimeError):
    """forecast_api 를 부를 수 없거나, 응답으로 세 target 이 맞는 기준 예측을 만들 수 없다."""


@dataclass(frozen=True)
class ForecastConfig:
    """config/stochastic.yaml 의 forecast 블록."""

    history_days: int
    horizon_h: int
    lead_h: int
    timeout_s: float

    @property
    def steps(self) -> int:
        """forecast_api 에 요청할 예측 길이: 결정 시각 + 1h 부터 계획일 23시까지."""
        return self.lead_h + self.horizon_h - 1


def load_config(path: Path | None = None) -> ForecastConfig:
    raw = read_stochastic_yaml(path)["forecast"]
    return ForecastConfig(
        history_days=int(cfg_value(raw["history"], "day")),
        horizon_h=int(cfg_value(raw["horizon"], "h")),
        lead_h=int(cfg_value(raw["lead"], "h")),
        timeout_s=float(cfg_value(raw["timeout"], "s")),
    )


@dataclass(frozen=True)
class TargetForecast:
    """target 하나의 예측. 배열 길이는 모두 같다."""

    times: list[datetime]
    predicted: list[float]
    margin: list[float]  # 90% 구간 반폭 = upper - predicted (lower 는 0 에서 잘려 있어 쓰지 않는다)


def _post(payload: dict, timeout_s: float) -> dict:
    url = os.getenv("FORECAST_URL", "http://forecast_api:8001") + f"{API_PREFIX}/forecast"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 (내부 URL)
        return json.load(resp)


def _to_kst(text: str) -> datetime:
    """forecast_api 의 시각은 tz 없는 한국 시각이다 (CLAUDE.md 규칙 11: aware 로 바꾼다)."""
    ts = datetime.fromisoformat(text)
    return ts.replace(tzinfo=KST) if ts.tzinfo is None else ts.astimezone(KST)


def fetch(end: datetime, cfg: ForecastConfig) -> dict[str, TargetForecast]:
    """end 시각까지의 데이터로 세 target 을 cfg.steps 시간 예측한다."""
    end = end.astimezone(KST)
    payload = {
        "start_date": (end - timedelta(days=cfg.history_days)).date().isoformat(),
        "end_date": end.strftime("%Y-%m-%d %H:%M:%S"),
        "target": "all",
        "horizon_hours": cfg.steps,
    }
    try:
        body = _post(payload, cfg.timeout_s)
    except (OSError, ValueError) as e:  # 연결 실패·타임아웃·HTTP 오류·JSON 아님
        raise ForecastUnavailableError(f"forecast_api 호출 실패: {e}") from e
    out = {}
    try:
        for target in TARGETS:
            points = body["forecasts"][target]["points"]
            if len(points) < cfg.steps:
                raise ForecastUnavailableError(f"{target} 예측이 {len(points)}시간뿐이다")
            out[target] = TargetForecast(
                times=[_to_kst(p["datetime"]) for p in points],
                predicted=[float(p["predicted"]) for p in points],
                # forecast_api 가 소수 1자리로 반올림해 보내므로 차이도 1자리로 맞춘다
                margin=[round(float(p["upper_bound"]) - float(p["predicted"]), 1) for p in points],
            )
    except (KeyError, TypeError, ValueError) as e:
        raise ForecastUnavailableError(f"forecast_api 응답 형식 오류: {e!r}") from e
    return out


def decision_time(data_end: datetime, lead_h: int) -> datetime:
    """data_end 이전의 가장 늦은 결정 시각. 결정 시각 + lead 가 자정이 되도록 고른다."""
    hour = (-lead_h) % 24
    cut = data_end.replace(hour=hour, minute=0, second=0, microsecond=0)
    return cut if cut <= data_end else cut - timedelta(days=1)


def latest_base(now: datetime | None = None, cfg: ForecastConfig | None = None) -> ScenarioProfile:
    """세 target 이 모두 있는 가장 최근 계획일의 기준 예측 + 90% 구간 반폭.

    못 만들면 ForecastUnavailableError.
    """
    cfg = cfg or load_config()
    now = (now or datetime.now(KST)).astimezone(KST).replace(minute=0, second=0, microsecond=0)
    probe = fetch(now, cfg)
    data_end = min(probe[t].times[0] for t in TARGETS) - HOUR  # 세 target 공통의 데이터 끝
    issued = decision_time(data_end, cfg.lead_h)
    start = issued + timedelta(hours=cfg.lead_h)
    logger.info("Day-ahead 기준 예측: 데이터 %s 까지 → 계획일 %s (lead %dh)",
                issued, start.date(), cfg.lead_h)

    series = fetch(issued, cfg)
    first = cfg.lead_h - 1  # issued + 1h 가 0번째 → issued + lead 가 (lead - 1)번째
    window = slice(first, first + cfg.horizon_h)
    expected = [start + h * HOUR for h in range(cfg.horizon_h)]
    for target in TARGETS:
        if series[target].times[window] != expected:
            got = series[target].times[first].isoformat()
            raise ForecastUnavailableError(
                f"{target} 예측 구간이 {start.isoformat()} 부터가 아니다 ({got})"
            )

    def part(target: str, field: str, floor: bool = False) -> list[float]:
        values = getattr(series[target], field)[window]
        # 태양광·풍력의 음수 예측은 출력 0 으로 본다
        return [max(v, 0.0) for v in values] if floor else list(values)

    return ScenarioProfile(
        demand_mw=part("demand", "predicted"),
        solar_mw=part("solar", "predicted", floor=True),
        wind_mw=part("wind", "predicted", floor=True),
        demand_margin_mw=part("demand", "margin"),
        solar_margin_mw=part("solar", "margin"),
        wind_margin_mw=part("wind", "margin"),
        start=start,
        issued_at=issued,
        source="forecast_api",
    )


def resolve_base(now: datetime | None = None) -> ScenarioProfile:
    """forecast_api 기준 예측. 쓸 수 없으면 config 의 base_profile (source="config")."""
    try:
        return latest_base(now)
    except ForecastUnavailableError as e:
        logger.warning("forecast_api 기준 예측을 쓸 수 없어 base_profile 로 폴백한다: %s", e)
        return DEFAULT_BASE
