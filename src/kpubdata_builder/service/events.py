"""Run event timeline HTTP API 서비스 로직 (#496).

``GET /builds/{run_id}/events``가 쓰는 순수 조회/직렬화 로직을 담는다.
run_id 형식 검증·존재 확인·ownership 게이팅은 service/app.py의
dispatch/BuilderService가 먼저 처리하고, 이 모듈은 그 뒤(신뢰된 run_id)부터
시작한다 — ``service.stages``와 동일한 책임 분리다.
"""

from __future__ import annotations

from ..events import BuildEvent
from ..spec import JsonValue

# limit query parameter의 bounded 기본값/상한. 다른 route(``service.stages``의
# DEFAULT_STAGE_PREVIEW_LIMIT/MAX_STAGE_PREVIEW_LIMIT)와 동일한 관례 — 상수를
# 한 곳에만 두고 route/service 양쪽에서 재사용한다(magic number 중복 금지).
DEFAULT_EVENTS_LIMIT = 200
MAX_EVENTS_LIMIT = 1000


def event_to_json(event: BuildEvent) -> dict[str, JsonValue]:
    """``BuildEvent``를 wire JSON으로 변환한다."""
    return {
        "seq": event.seq,
        "timestamp": event.timestamp.isoformat(),
        "run_id": event.run_id,
        "event": event.event,
        "status": event.status,
        "source_key": event.source_key,
        "stage": event.stage,
        "message": event.message,
        "metrics": dict(event.metrics) if event.metrics is not None else None,
    }


__all__ = ["DEFAULT_EVENTS_LIMIT", "MAX_EVENTS_LIMIT", "event_to_json"]
