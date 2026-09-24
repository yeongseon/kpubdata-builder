"""인증 실패 스로틀 — 실패한 인증 시도의 반복 비용을 클라이언트에게 지운다.

인증 게이트(``dispatch``)는 요청마다 API 키 비교 또는 ID token의 RS256 서명 검증을
수행한다. 실패한 시도를 무제한으로 받아주면 두 가지가 공짜가 된다:

1. 인스턴스당 하나뿐인 정적 ``X-API-Key`` 에 대한 추측 시도 (ADR 0006).
2. 무효 토큰을 던져 JWKS 서명 검증 CPU를 소모시키는 행위 — 검증 자체는 오프라인이라
   외부 호출은 없지만 공개 엔드포인트에서 반복되면 CPU가 그대로 나간다.

같은 클라이언트가 윈도 안에서 한도를 넘게 실패하면 **인증을 시도하기 전에** 429로
끊는다. 성공한 인증은 그 클라이언트의 실패 기록을 즉시 비운다 — 정상 사용자가
토큰 만료로 몇 번 401을 받는 것은 카운터에 누적되지 않는다.

기본값은 정상 클라이언트가 닿을 수 없게 넉넉히 잡았다(60초에 60회 실패). 배포에서
조정하거나 끄려면 ``KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT`` 를 쓴다(0 이하이면 비활성).

**클라이언트 식별은 TCP peer 주소다.** ``X-Forwarded-For`` 는 위조 가능하므로 읽지
않는다. 따라서 Builder가 클라이언트 IP를 보존하지 않는 리버스 프록시 뒤에 있으면 모든
요청이 한 버킷을 공유한다 — 그런 배포에서는 스로틀을 프록시 계층에서 걸고 여기서는
비활성화하는 편이 낫다(deploy.md).
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections import deque
from collections.abc import Callable

_LIMIT_ENV = "KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT"
_WINDOW_ENV = "KPUBDATA_BUILDER_AUTH_FAILURE_WINDOW_SECONDS"

_DEFAULT_LIMIT = 60
_DEFAULT_WINDOW_SECONDS = 60.0
# 추적 대상 클라이언트 수 상한 — 스로틀 자체가 메모리 증폭 벡터가 되지 않게 한다.
# 서로 다른 IP로 실패를 뿌리는 공격자가 임의 크기의 dict를 만들지 못한다.
_DEFAULT_MAX_CLIENTS = 4096


def _positive_int_env(name: str, default: int) -> int:
    """env를 int로 읽되, 비거나 형식이 틀리면 기본값을 쓴다(기동 실패로 만들지 않는다)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


class AuthFailureThrottle:
    """클라이언트별 인증 실패를 슬라이딩 윈도로 세는 in-process 스로틀.

    ``BuilderService`` 인스턴스마다 하나씩 두어 테스트 간 상태가 섞이지 않게 한다
    (``LatencyRecorder`` 와 동일한 이유). 프로세스 로컬이라 다중 인스턴스 배포에서는
    인스턴스별로 카운트된다 — 정확한 전역 한도가 아니라 남용 완화가 목적이다.
    """

    def __init__(
        self,
        *,
        limit: int | None = None,
        window_seconds: float | None = None,
        max_clients: int = _DEFAULT_MAX_CLIENTS,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit if limit is not None else _positive_int_env(_LIMIT_ENV, _DEFAULT_LIMIT)
        self._window = (
            window_seconds
            if window_seconds is not None
            else _positive_float_env(_WINDOW_ENV, _DEFAULT_WINDOW_SECONDS)
        )
        self._max_clients = max_clients
        self._now = time_source
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """한도가 0 이하이면 비활성 — 모든 호출이 no-op이 된다."""
        return self._limit > 0

    def retry_after(self, client_id: str | None) -> int | None:
        """지금 이 클라이언트를 막아야 하면 남은 대기 시간(초, 올림)을 반환한다.

        막을 필요가 없으면 ``None``. 클라이언트를 식별할 수 없으면(``client_id`` 가
        ``None``) 스로틀하지 않는다 — 식별 불가를 이유로 정상 요청을 막지 않는다.
        """
        if not self.enabled or client_id is None:
            return None
        with self._lock:
            timestamps = self._failures.get(client_id)
            if timestamps is None:
                return None
            now = self._now()
            self._prune(timestamps, now)
            if not timestamps:
                del self._failures[client_id]
                return None
            if len(timestamps) < self._limit:
                return None
            # 가장 오래된 실패가 윈도 밖으로 나가야 다시 여유가 생긴다.
            remaining = timestamps[0] + self._window - now
            return max(1, math.ceil(remaining))

    def record_failure(self, client_id: str | None) -> None:
        """인증 실패 1건을 기록한다(401 계열만 — 403/503은 호출부에서 제외한다)."""
        if not self.enabled or client_id is None:
            return
        with self._lock:
            now = self._now()
            timestamps = self._failures.get(client_id)
            if timestamps is None:
                self._evict_if_needed(now)
                timestamps = deque()
                self._failures[client_id] = timestamps
            self._prune(timestamps, now)
            timestamps.append(now)

    def record_success(self, client_id: str | None) -> None:
        """인증 성공 — 해당 클라이언트의 실패 기록을 비운다."""
        if not self.enabled or client_id is None:
            return
        with self._lock:
            self._failures.pop(client_id, None)

    def _prune(self, timestamps: deque[float], now: float) -> None:
        cutoff = now - self._window
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

    def _evict_if_needed(self, now: float) -> None:
        """추적 대상이 상한에 닿으면 만료분을 먼저 버리고, 그래도 넘치면 가장 오래된 것을 버린다."""
        if len(self._failures) < self._max_clients:
            return
        cutoff = now - self._window
        expired = [
            key for key, stamps in self._failures.items() if not stamps or stamps[-1] <= cutoff
        ]
        for key in expired:
            del self._failures[key]
        while len(self._failures) >= self._max_clients:
            oldest = min(self._failures, key=lambda key: self._failures[key][-1])
            del self._failures[oldest]


__all__ = ["AuthFailureThrottle"]
