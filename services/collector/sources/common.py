"""sources 공용 유틸. 키 로드, 재시도, 응답 꺼내기, 시각 포맷."""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta
from urllib.parse import unquote

import requests

import domain

logger = logging.getLogger(__name__)

TIMEOUT = int(os.getenv("HTTP_TIMEOUT_SEC", "30"))
RETRIES = int(os.getenv("HTTP_RETRIES", "3"))
BACKOFF_SEC = 2.0


def service_key() -> str:
    """공공데이터포털 키. .env 에 인코딩된 채로 들어있으면 풀어서 쓴다.

    requests 가 파라미터를 다시 인코딩하므로, 이미 %2B 같은 꼴이면 이중 인코딩이 된다.
    """
    key = os.environ["DATA_GO_KR_SERVICE_KEY"]
    return unquote(key) if "%" in key else key


def kma_auth_key() -> str:
    return os.environ["KMA_AUTH_KEY"]


def get_json(url: str, params: dict) -> dict:
    """JSON 응답. 실패하면 재시도하고, 끝내 실패하면 예외를 올린다."""
    res = _request(url, params)
    try:
        return res.json()
    except ValueError as exc:
        # 키 오류·한도 초과 때 XML 에러 문서가 내려온다. 앞부분만 로그에 남긴다.
        raise RuntimeError(f"JSON 이 아닌 응답: {res.text[:200]}") from exc


def get_text(url: str, params: dict) -> str:
    """텍스트 응답 (기상청)."""
    return _request(url, params).text


def _request(url: str, params: dict) -> requests.Response:
    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            res = requests.get(url, params=params, timeout=TIMEOUT)
            res.raise_for_status()
            return res
        except Exception as exc:  # 기상청은 504 가 잦아 재시도가 필수다
            last = exc
            if attempt < RETRIES:
                wait = BACKOFF_SEC * attempt
                logger.warning("%s 요청 실패(%d/%d): %s — %.0f초 후 재시도",
                               url, attempt, RETRIES, _mask(str(exc)), wait)
                time.sleep(wait)
    raise RuntimeError(f"{url} 요청 실패: {_mask(str(last))}") from last


def _mask(text: str) -> str:
    """예외 메시지에 URL 째로 들어오는 인증키를 로그에서 가린다."""
    for env_name in ("DATA_GO_KR_SERVICE_KEY", "KMA_AUTH_KEY"):
        key = os.getenv(env_name)
        if key:
            text = text.replace(key, "***").replace(unquote(key), "***")
    return text


def items(payload: dict) -> list[dict]:
    """공공데이터포털 응답에서 response.body.items.item 만 꺼낸다.

    건수가 1이면 dict 로, 0이면 빈 문자열로 오는 경우가 있어 리스트로 맞춰준다.
    """
    body = (payload.get("response") or {}).get("body") or {}
    item = (body.get("items") or {})
    if isinstance(item, dict):
        item = item.get("item")
    if item is None or item == "":
        return []
    return item if isinstance(item, list) else [item]


def total_count(payload: dict) -> int:
    body = (payload.get("response") or {}).get("body") or {}
    try:
        return int(body.get("totalCount", 0))
    except (TypeError, ValueError):
        return 0


def check_result(payload: dict, url: str) -> None:
    """resultCode 가 정상이 아니면 예외. 빈 응답과 키 오류를 구분하려고 본다."""
    header = (payload.get("response") or {}).get("header") or {}
    code = str(header.get("resultCode", "00"))
    if code not in ("00", "0"):
        raise RuntimeError(f"{url} 응답 오류 {code}: {header.get('resultMsg')}")


def fmt_ts(ts: datetime) -> str:
    return ts.strftime(domain.TS_FORMAT)


def days(start: datetime, end: datetime) -> list[date]:
    """start ~ end 를 하루 단위로 쪼갠다. 날짜 파라미터를 쓰는 API 용."""
    out: list[date] = []
    cur = start.date()
    last = end.date()
    while cur <= last:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def num(token: str) -> float | None:
    """숫자로 바꾼다. 결측 표기나 빈 값이면 None."""
    token = (token or "").strip().replace(",", "")
    if not token or token in domain.KMA_MISSING:
        return None
    try:
        return float(token)
    except ValueError:
        return None
