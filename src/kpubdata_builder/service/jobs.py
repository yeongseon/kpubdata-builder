"""비동기 build job 레지스트리와 bounded worker 실행기 (#482)."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import uuid4

from ..spec import JsonValue

BuildJobStatus = Literal["queued", "running", "cancelling", "succeeded", "failed", "cancelled"]


if TYPE_CHECKING:
    from .app import ServiceResponse as BuildJobResponse
else:

    class BuildJobResponse(Protocol):
        """BuilderService 응답 중 job 실행기가 필요한 구조."""

        status_code: int
        body: dict[str, JsonValue]


BuildJobRunner = Callable[[str, str, str | None], BuildJobResponse]
SubmitStatus = Literal["accepted", "existing", "queue_full"]


@dataclass(frozen=True, slots=True)
class BuildJobSnapshot:
    """외부에 노출 가능한 build job 상태 스냅샷."""

    run_id: str
    status: BuildJobStatus
    created_at: str
    updated_at: str
    created_by: str | None = None
    response: dict[str, JsonValue] | None = None
    error: str | None = None

    def to_body(self) -> dict[str, JsonValue]:
        body: dict[str, JsonValue] = {
            "run_id": self.run_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.created_by is not None:
            body["created_by"] = self.created_by
        if self.response is not None:
            body["response"] = self.response
        if self.error is not None:
            body["error"] = self.error
        return body


@dataclass(frozen=True, slots=True)
class BuildJobSubmitResult:
    status: SubmitStatus
    snapshot: BuildJobSnapshot | None = None


class AsyncBuildJobRegistry:
    """프로세스 메모리에만 유지되는 active/terminal job 레지스트리."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._jobs: dict[str, BuildJobSnapshot] = {}

    def create(self, *, run_id: str, created_by: str | None) -> BuildJobSnapshot:
        now = _utc_now_text()
        snapshot = BuildJobSnapshot(
            run_id=run_id,
            status="queued",
            created_at=now,
            updated_at=now,
            created_by=created_by,
        )
        with self._lock:
            self._jobs[run_id] = snapshot
        return snapshot

    def mark_running(self, run_id: str) -> BuildJobSnapshot:
        return self._replace(run_id, status="running")

    def mark_succeeded(self, run_id: str, response: dict[str, JsonValue]) -> BuildJobSnapshot:
        return self._replace(run_id, status="succeeded", response=response)

    def mark_failed(
        self, run_id: str, *, response: dict[str, JsonValue] | None = None, error: str | None = None
    ) -> BuildJobSnapshot:
        return self._replace(run_id, status="failed", response=response, error=error)

    def get(self, run_id: str) -> BuildJobSnapshot | None:
        with self._lock:
            return self._jobs.get(run_id)

    def queued_count(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values() if job.status == "queued")

    def _replace(
        self,
        run_id: str,
        *,
        status: BuildJobStatus,
        response: dict[str, JsonValue] | None = None,
        error: str | None = None,
    ) -> BuildJobSnapshot:
        with self._lock:
            current = self._jobs[run_id]
            updated = BuildJobSnapshot(
                run_id=current.run_id,
                status=status,
                created_at=current.created_at,
                updated_at=_utc_now_text(),
                created_by=current.created_by,
                response=response,
                error=error,
            )
            self._jobs[run_id] = updated
            return updated


class AsyncBuildExecutor:
    """고정 크기 worker pool로 build job을 실행한다."""

    def __init__(self, *, max_workers: int, max_queue_size: int = 10) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="kpubdata-build"
        )
        self._max_queue_size = max_queue_size
        self.registry = AsyncBuildJobRegistry()

    def submit(
        self,
        *,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        runner: BuildJobRunner,
    ) -> BuildJobSubmitResult:
        existing = self.registry.get(run_id)
        if existing is not None:
            return BuildJobSubmitResult(status="existing", snapshot=existing)
        if self.registry.queued_count() >= self._max_queue_size:
            return BuildJobSubmitResult(status="queue_full")
        snapshot = self.registry.create(run_id=run_id, created_by=created_by)
        self._executor.submit(self._run, spec_yaml, run_id, created_by, runner)
        return BuildJobSubmitResult(status="accepted", snapshot=snapshot)

    def get(self, run_id: str) -> BuildJobSnapshot | None:
        return self.registry.get(run_id)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        runner: BuildJobRunner,
    ) -> None:
        self.registry.mark_running(run_id)
        try:
            response = runner(spec_yaml, run_id, created_by)
        except RuntimeError as exc:
            self.registry.mark_failed(run_id, error=str(exc))
            return
        if response.status_code < 400:
            self.registry.mark_succeeded(run_id, response=response.body)
            return
        error = response.body.get("error")
        self.registry.mark_failed(
            run_id,
            response=response.body,
            error=error if isinstance(error, str) else "build failed",
        )


def _utc_now_text() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def generate_run_id() -> str:
    return f"{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:12]}"


__all__ = [
    "AsyncBuildExecutor",
    "AsyncBuildJobRegistry",
    "BuildJobSubmitResult",
    "BuildJobResponse",
    "BuildJobSnapshot",
    "BuildJobStatus",
    "SubmitStatus",
    "generate_run_id",
]
