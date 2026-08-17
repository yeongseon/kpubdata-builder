"""Run 단위 structured event timeline (#496).

Build 실행 과정(run/source fetch/medallion stage/quality checkpoint)을 raw
logger parsing 없이 조회할 수 있도록 append-only event timeline을 제공한다.

주요 구성:
    - BuildEvent: 단일 structured event 모델
    - BuildEventStore: append-only SQLite 저장소 (event timeline의 정본)
    - BuildEventRecorder: pipeline이 안전하게 event를 남기기 위한 wrapper
"""

from __future__ import annotations

from .models import (
    BuildEvent,
    EventName,
    EventStatus,
    QualityEventName,
    RunEventName,
    SourceFetchEventName,
    StageEventName,
    StageName,
)
from .recorder import BuildEventRecorder
from .store import BuildEventStore

__all__ = [
    "BuildEvent",
    "BuildEventRecorder",
    "BuildEventStore",
    "EventName",
    "EventStatus",
    "QualityEventName",
    "RunEventName",
    "SourceFetchEventName",
    "StageEventName",
    "StageName",
]
