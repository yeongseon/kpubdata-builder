"""Run 단위 structured event timeline 모델 (#496).

Build 실행 과정을 raw logger 파싱 없이 조회할 수 있도록, run 안에서 일어나는
주요 전이(run/source fetch/stage/quality)를 명시적 구조화 모델로 표현한다.

원칙:
    - event/status/stage 어휘는 bounded(Literal)하고 deterministic하다. 임의
      logger message 문자열을 API 계약으로 승격하지 않는다.
    - arbitrary object dumping, exception 객체 직렬화, stack trace, raw
      provider response, raw path, credential은 이 모델에 담지 않는다 — 호출부
      (``events.recorder``/``pipeline.orchestrator``)가 이미 안전하다고 확인한
      값만 여기로 들어온다.
    - ``seq``는 store가 append 시점에 부여하는 monotonic ordering identifier다
      (#496) — 병렬 source worker가 동시에 append해도 store가 부여한 전역 순서를
      그대로 신뢰할 수 있다. append 전에는 placeholder(0)다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ..spec.models import JsonValue

# run 수준 전이. "cancelled"는 아직 이 저장소에 실제 cancellation 전이가 없어
# (#481 미구현) 어휘에 포함하지 않는다 — 그 전이가 생기면 함께 추가한다.
RunEventName = Literal["run_submitted", "run_started", "run_finished", "run_failed"]

# source fetch 전이 (#498 resolver 경계: public_api/file/url 공통).
SourceFetchEventName = Literal[
    "source_fetch_started", "source_fetch_completed", "source_fetch_failed"
]

# medallion stage 전이. "export"는 BuildSpec.exports 실행 단계다.
StageEventName = Literal["stage_started", "stage_completed", "stage_failed"]

# quality/schema 평가 체크포인트 (#486 결과를 재판정하지 않고 그대로 반영).
QualityEventName = Literal["quality_evaluated"]

EventName = RunEventName | SourceFetchEventName | StageEventName | QualityEventName

# event의 결과. "ok"는 성공/정상 완료, "warn"은 quality_evaluated에서 WARN이
# 하나 이상 있었지만 FAIL은 없었던 경우, "fail"은 실패다. run/source/stage 전이는
# ok 또는 fail만 쓴다(started는 ok, completed는 ok, failed는 fail).
EventStatus = Literal["ok", "warn", "fail"]

StageName = Literal["bronze", "silver", "gold", "export"]


@dataclass(frozen=True, slots=True)
class BuildEvent:
    """단일 structured run event.

    속성:
        seq: store가 append 시점에 부여하는 monotonic ordering identifier.
            append 전에는 0(placeholder) — ``BuildEventStore.append()``가 실제
            값을 채운 새 인스턴스를 반환한다.
        timestamp: timezone-aware UTC 시각.
        run_id: 이 event가 속한 run.
        event: 어떤 전이가 일어났는지 (bounded vocabulary).
        status: 그 전이의 결과 (ok/warn/fail).
        source_key: 관련 소스 식별자. run 수준 event는 None.
        stage: 관련 medallion stage. source fetch/run 수준 event는 None.
        message: 사람이 읽는 bounded, 안전한 요약. 없으면 None — 임의 값을
            지어내지 않는다.
        metrics: JSON 직렬화 가능한 안전한 수치 요약(예: rows/check_count).
            원본 provider 응답이나 임의 object를 담지 않는다.
    """

    seq: int
    timestamp: datetime
    run_id: str
    event: EventName
    status: EventStatus
    source_key: str | None = None
    stage: StageName | None = None
    message: str | None = None
    metrics: Mapping[str, JsonValue] | None = None


__all__ = [
    "BuildEvent",
    "EventName",
    "EventStatus",
    "QualityEventName",
    "RunEventName",
    "SourceFetchEventName",
    "StageEventName",
    "StageName",
]
