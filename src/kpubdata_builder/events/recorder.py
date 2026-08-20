"""Pipeline이 event를 안전하게 기록하기 위한 얇은 wrapper (#496).

``pipeline.orchestrator``가 이 recorder를 통해서만 event를 남긴다 — store를
직접 건드리지 않는다. 두 가지를 이 계층에서 강제한다.

1. **message 길이 상한**: 어떤 호출부든 bounded, 안전한 message만 넘기지만,
   방어적으로 한 곳에서 길이를 자른다(길이만 자르므로 새로운 정보를 만들어
   내거나 숨겨진 내용을 드러내지 않는다).
2. **append 실패가 이미 진행 중인 build의 실행/결과를 바꾸지 않는다**:
   ``BuildEventStore`` 자체는 실패를 삼키지 않는다(#496, 이 store가 event의
   유일한 정본) — 그러나 이 recorder가 감싸는 호출부는 대부분 실제 side
   effect(bronze/silver/gold persist, source fetch)가 *이미 일어난 뒤*
   호출된다(``stage_completed``, ``run_finished`` 등). 여기서 예외를 그대로
   전파하면 event 저장소라는 **다른 subsystem**의 일시적 장애가 이미 성공한
   소스를 실패로 뒤집거나(``_run_source_pipeline``의 공통 except가 모든
   예외를 "이 소스 실패"로 해석한다), manifest 기록 전에 run_build를 중단시켜
   ``manifest.json``이 아예 안 만들어지는 상태(AGENTS.md '매니페스트 누락
   금지' 위반)를 만들 수 있다. 이건 ``BuildIndex`` write가 best-effort인
   이유(ADR 0003 — 인덱스가 파생·재구축 가능하기 때문)와는 **다른** 이유다:
   여기서는 event store가 파생물이라서가 아니라, event 기록 실패가 *다른
   정본*(manifest/소스 outcome)을 침범하면 안 되기 때문에 흡수한다.
   그렇다고 완전히 조용히 삼키지도 않는다 — ``logger.error``로 남기는 것에
   더해, 실패한 호출을 ``dropped_events()``로 누적해 호출부(orchestrator)가
   기존 ``BuildManifest.warnings``(#496 이전부터 존재하는, 아직 아무도 채우지
   않던 authoritative 필드)에 실을 수 있게 한다 — API 소비자는
   ``GET /builds/{run_id}/manifest``로 이 run의 event timeline에 실제로 구멍이
   있었는지 확인할 수 있다. 새 API 필드나 새 상태값을 추가하지 않는다.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import cast

from ..quality.models import QualityCheckResult
from ..spec.models import JsonValue
from .models import BuildEvent, EventName, EventStatus, StageName
from .store import BuildEventStore

logger = logging.getLogger(__name__)

# event message 방어적 상한. 임의로 큰 quality 실패 메시지 나열 등이 API/저장소를
# 무제한으로 부풀리지 않게 한다. 길이만 자르므로 숨겨진 내용을 새로 드러내지 않는다.
_MAX_MESSAGE_LENGTH = 500


def _bounded_message(message: str | None) -> str | None:
    if message is None:
        return None
    if len(message) <= _MAX_MESSAGE_LENGTH:
        return message
    return message[:_MAX_MESSAGE_LENGTH] + "…"


def _quality_status(results: Sequence[QualityCheckResult]) -> EventStatus:
    if any(r.status == "fail" for r in results):
        return "fail"
    if any(r.status == "warn" for r in results):
        return "warn"
    return "ok"


class BuildEventRecorder:
    """단일 run에 바인딩된, 절대 예외를 전파하지 않는 event 기록기.

    ``store``가 ``None``이면(이벤트 저장소 없이 ``run_build``를 직접 호출하는
    CLI/저수준 테스트 경로) 모든 메서드가 아무 것도 하지 않는다 — pipeline
    코드가 recorder 유무를 매번 분기할 필요가 없다.

    여러 source가 ``ThreadPoolExecutor``(#247)에서 동시에 같은 recorder
    인스턴스로 event를 남기므로, ``dropped_events()``로 누적되는 실패 목록도
    lock으로 보호한다.
    """

    def __init__(self, store: BuildEventStore | None, *, run_id: str) -> None:
        self._store = store
        self._run_id = run_id
        self._dropped_lock = threading.Lock()
        self._dropped_events: list[str] = []

    def dropped_events(self) -> tuple[str, ...]:
        """append에 실패한 event를 설명하는 bounded 경고 목록을 반환한다 (#496).

        원본 예외 메시지(sqlite 오류 텍스트 등에 내부 경로가 섞일 수 있다)는
        담지 않는다 — event/source_key/stage처럼 이미 안전하다고 확인된
        식별자만으로 구성한다. 호출부(``run_build``)가 이 값을
        ``BuildManifest.warnings``에 실어 API로 노출한다.
        """
        with self._dropped_lock:
            return tuple(self._dropped_events)

    def _record(
        self,
        event: str,
        status: EventStatus,
        *,
        source_key: str | None = None,
        stage: StageName | None = None,
        message: str | None = None,
        metrics: Mapping[str, JsonValue] | None = None,
    ) -> None:
        if self._store is None:
            return
        built = BuildEvent(
            seq=0,
            timestamp=datetime.now(tz=timezone.utc),
            run_id=self._run_id,
            event=cast(EventName, event),
            status=status,
            source_key=source_key,
            stage=stage,
            message=_bounded_message(message),
            metrics=metrics,
        )
        try:
            self._store.append(built)
        except Exception:
            logger.error(
                "build event append failed (run_id=%s, event=%s, source_key=%s, stage=%s); "
                "recorded as a manifest warning instead of failing the build (#496)",
                self._run_id,
                event,
                source_key,
                stage,
                exc_info=True,
            )
            detail = f"event recording failed: {event}"
            if source_key is not None:
                detail += f" (source_key={source_key})"
            if stage is not None:
                detail += f" (stage={stage})"
            with self._dropped_lock:
                self._dropped_events.append(detail)

    # --- run lifecycle -------------------------------------------------
    #
    # "run_submitted"는 여기 없다 — 이 recorder의 흡수(never-raise) 정책이
    # 맞지 않는 유일한 event다: 이 event가 기록될 때는 아직 job이 executor에
    # 큐잉되기 전이라(#496) 실제 side effect가 없고, 그래서 실패를 그대로
    # 전파해도 다른 정본을 침범하지 않는다(오히려 job을 아예 큐잉하지 않게
    # 막아야 한다). ``BuilderService.submit_build``가
    # ``AsyncBuildExecutor.submit(on_accept=...)`` hook에서 store에 직접
    # append해 그 전파를 그대로 활용한다.
    #
    # "run_cancelled"(#481)도 여기 없다 — 취소된 job의 terminal 상태를 실제로
    # 확정하는 곳은 service의 job registry이고(queued 취소는 pipeline이 아예
    # 실행되지 않아 recorder 자체가 존재하지 않는다), 그 한 곳에서만 남겨야
    # 종결 event가 run당 정확히 하나가 된다. pipeline은 취소를 관찰하면
    # run_finished/run_failed를 남기지 않는 것으로 자기 몫을 다한다.

    def run_started(self) -> None:
        self._record("run_started", "ok", message="pipeline execution started")

    def run_finished(self) -> None:
        self._record("run_finished", "ok", message="build completed")

    def run_failed(self, *, failed_source_count: int) -> None:
        self._record("run_failed", "fail", message=f"{failed_source_count} source(s) failed")

    # --- source fetch (#498 resolver 경계: public_api/file/url 공통) ---

    def source_fetch_started(self, source_key: str) -> None:
        self._record("source_fetch_started", "ok", source_key=source_key)

    def source_fetch_completed(self, source_key: str, *, record_count: int) -> None:
        self._record(
            "source_fetch_completed",
            "ok",
            source_key=source_key,
            message="source fetched",
            metrics={"records": record_count},
        )

    def source_fetch_failed(self, source_key: str, *, message: str) -> None:
        self._record("source_fetch_failed", "fail", source_key=source_key, message=message)

    # --- medallion stage -------------------------------------------------

    def stage_started(self, source_key: str, stage: StageName) -> None:
        self._record("stage_started", "ok", source_key=source_key, stage=stage)

    def stage_completed(
        self,
        source_key: str,
        stage: StageName,
        *,
        message: str,
        metrics: Mapping[str, JsonValue] | None = None,
    ) -> None:
        self._record(
            "stage_completed",
            "ok",
            source_key=source_key,
            stage=stage,
            message=message,
            metrics=metrics,
        )

    def stage_failed(self, source_key: str, stage: StageName, *, message: str) -> None:
        self._record("stage_failed", "fail", source_key=source_key, stage=stage, message=message)

    # --- quality/schema checkpoint (#486 결과를 그대로 반영, 재판정하지 않음) --

    def quality_evaluated(self, source_key: str, results: Sequence[QualityCheckResult]) -> None:
        pass_count = sum(1 for r in results if r.status == "pass")
        warn_count = sum(1 for r in results if r.status == "warn")
        fail_count = sum(1 for r in results if r.status == "fail")
        self._record(
            "quality_evaluated",
            _quality_status(results),
            source_key=source_key,
            metrics={
                "check_count": len(results),
                "pass_count": pass_count,
                "warn_count": warn_count,
                "fail_count": fail_count,
            },
        )


__all__ = ["BuildEventRecorder"]
