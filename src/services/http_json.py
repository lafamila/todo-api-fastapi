"""동기화 경로가 쓰는 최소 JSON HTTP 클라이언트 (stdlib only).

`requests` 를 새 의존성으로 들이지 않는다 — 기존 `session_auth.py` 도 `urllib` 로
auth 와 통신한다. 이 모듈은 그 패턴을 동기화용으로 재사용 가능하게 뽑은 것이다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import error, request


@dataclass
class JsonResponse:
    status: int
    data: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class HttpUnreachable(Exception):
    """네트워크 자체가 닿지 않았다 — 오프라인 판정의 근거."""


def _decode(raw: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def request_json(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 15,
) -> JsonResponse:
    """JSON 요청/응답. HTTP 에러 상태는 예외가 아니라 status 로 돌려준다.

    연결 실패(DNS/타임아웃/거부)만 `HttpUnreachable` 로 올린다 — 호출자가
    "오프라인" 과 "서버가 거절함" 을 구분해야 하기 때문이다.
    """
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = request.Request(url, data=payload, method=method.upper())
    req.add_header("Accept", "application/json")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)

    try:
        with request.urlopen(req, timeout=timeout) as response:
            return JsonResponse(response.status, _decode(response.read().decode("utf-8")))
    except error.HTTPError as exc:
        return JsonResponse(exc.code, _decode(exc.read().decode("utf-8")))
    except error.URLError as exc:
        raise HttpUnreachable(str(exc.reason)) from exc
    except TimeoutError as exc:  # pragma: no cover - urllib 가 socket.timeout 을 올리는 경로
        raise HttpUnreachable("timeout") from exc
