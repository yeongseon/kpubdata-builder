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
    """build job 상태 스냅샷.

    ``owner_id``는 active/terminal async run의 ownership 판정용 internal
    필드다(#496 follow-up, #505 canonical stable identity) — ``created_by``
    (Principal.label, display/legacy fallback)와 달리 신규 ownership 판정에
    우선 쓰이는 값이고, ``BuilderService._run_build_job``이 persisted
    manifest/BuildIndex(#505 SSOT) 기록용으로도 그대로 재사용한다. 다만
    ``to_body()``가 wire로 절대 내보내지 않고, ``kind="file"`` source
    resolver(#498)에는 여전히 전달되지 않는다 — async file-backed source
    owner propagation 한계는 그대로 유지된다.
    """

    run_id: str
    status: BuildJobStatus
    created_at: str
    updated_at: str
    created_by: str | None = None
    owner_id: str | None = None
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


@dataclass(frozen=True, slots=True)
class AsyncBuildJobCounts:
    """``snapshot_counts()``가 단일 lock scope에서 집계한 active job 수 (#516)."""

    queued: int
    running: int


class AsyncBuildJobRegistry:
    """프로세스 메모리에만 유지되는 active/terminal job 레지스트리."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._jobs: dict[str, BuildJobSnapshot] = {}

    def create(
        self, *, run_id: str, created_by: str | None, owner_id: str | None = None
    ) -> BuildJobSnapshot:
        now = _utc_now_text()
        snapshot = BuildJobSnapshot(
            run_id=run_id,
            status="queued",
            created_at=now,
            updated_at=now,
            created_by=created_by,
            owner_id=owner_id,
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

    def snapshot_counts(self) -> AsyncBuildJobCounts:
        """queued/running job 수를 단일 lock scope에서 일관되게 집계한다 (#516).

        ``queued_count()`` 같은 개별 메서드를 서로 다른 시점에 호출해 조합하면
        그 사이의 상태 전이(queued -> running)로 모순된 snapshot이 만들어질 수
        있다 — monitoring은 반드시 이 메서드처럼 하나의 lock 안에서 계산한
        값을 써야 한다. terminal(succeeded/failed/cancelled) job은 registry가
        메모리에 계속 보존하지만 이 snapshot에는 포함하지 않는다 — Monitoring이
        보여줘야 하는 건 현재 workload이지 terminal history가 아니다. mutable
        ``_jobs`` dict 자체는 절대 외부에 노출하지 않는다.
        """
        with self._lock:
            queued = 0
            running = 0
            for job in self._jobs.values():
                if job.status == "queued":
                    queued += 1
                elif job.status == "running":
                    running += 1
        return AsyncBuildJobCounts(queued=queued, running=running)

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
                owner_id=current.owner_id,
                response=response,
                error=error,
            )
            self._jobs[run_id] = updated
            return updated


@dataclass(frozen=True, slots=True)
class AsyncBuildStats:
    """Monitoring API(#516)가 소비하는 async build 실행기 raw 집계.

    ``queued``/``running``/``capacity`` raw 값만 담는다 — ``total``/``active``/
    ``utilization`` 같은 파생값과 availability 판정은 ``service/monitoring.py``의
    책임이다.
    """

    queued: int
    running: int
    capacity: int


class AsyncBuildExecutor:
    """고정 크기 worker pool로 build job을 실행한다."""

    def __init__(self, *, max_workers: int, max_queue_size: int = 10) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="kpubdata-build"
        )
        # ThreadPoolExecutor._max_workers 같은 private field를 외부(monitoring)가
        # 직접 읽지 않도록 capacity를 생성 시점에 명시적으로 보존한다 (#516).
        self._max_workers = max_workers
        self._max_queue_size = max_queue_size
        self.registry = AsyncBuildJobRegistry()

    def stats(self) -> AsyncBuildStats:
        """monitoring용 read-only aggregate snapshot (#516).

        registry의 mutable internal job dict는 노출하지 않고, 단일 lock scope
        에서 얻은 queued/running 집계(``registry.snapshot_counts()``)와 worker
        pool capacity만 반환한다. 이 호출 자체는 job 상태를 변경하지 않는다.
        """
        counts = self.registry.snapshot_counts()
        return AsyncBuildStats(
            queued=counts.queued, running=counts.running, capacity=self._max_workers
        )

    def submit(
        self,
        *,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        runner: BuildJobRunner,
        owner_id: str | None = None,
        on_accept: Callable[[], None] | None = None,
        on_enqueue_failure: Callable[[], None] | None = None,
    ) -> BuildJobSubmitResult:
        """job을 큐잉한다. ``existing``/``queue_full``이면 새 submission이 아니므로
        ``on_accept``를 호출하지 않는다.

        ``owner_id``는 ``registry.create()``까지만 전달되어 snapshot에
        보존된다(#496 follow-up, active run ownership 판정용) — ``runner``
        호출(아래 ``self._executor.submit(self._run, spec_yaml, run_id,
        created_by, runner)``)에는 전달하지 않는다. run_build/source resolver
        쪽 owner propagation은 #498에서 이미 별도 한계로 남겨졌고, 이번
        변경에서 그 범위를 넓히지 않는다.

        ``on_accept``는 job이 실제로 worker pool에 큐잉되기(``self._executor.submit``)
        *전*에 호출된다(#496). 호출자(``BuilderService.submit_build``)는 이 hook으로
        "run_submitted" event를 append한다 — 그 append가 여기서 실패해 예외를
        전파하면, job은 registry에 만들어지지도 worker에 큐잉되지도 않은 채
        그대로 끝난다. 이 순서 덕분에 "event는 유실됐는데 job은 이미 실행
        중"이라는 모순이 구조적으로 생기지 않는다 — job의 "accepted" 여부 자체가
        이 event 기록 성공에 달려 있다. ``on_accept``가 없는 호출자(기존
        monitoring 테스트 등)는 이전과 동일하게 아무 gating 없이 그대로 큐잉된다.

        반대 방향(#496 self-review): ``on_accept``가 성공해 event는 기록됐는데
        ``self._executor.submit()``(실제 worker pool 큐잉) 자체가 실패하면,
        ``registry.create()``가 이미 만든 "queued" 항목이 아무도 실행하지 않을
        phantom으로 영원히 남는다 — event(``run_submitted``)는 append-only라
        지울 수 없으므로(#496 원칙), 이 항목을 "queued"로 방치하지 않고 실제
        job 실행 실패에 이미 쓰이는 것과 동일한 ``registry.mark_failed()``로
        정리한 뒤 예외를 그대로 전파한다 — 새 상태값을 만들지 않고 기존
        terminal mechanism을 재사용한다.

        ``on_enqueue_failure``는 ``registry.mark_failed()`` 직후, 예외를
        재전파하기 *전*에 호출된다(#496 lifecycle 계약: timeline 자체도 이
        실패를 표현해야 한다) — 호출자는 이 hook으로 같은 run_id에 기존
        ``run_failed`` event를 append한다. ``on_accept`` 실패 경로(event 자체가
        전혀 기록되지 못한 경우)와는 구별된다 — 그 경로는 ``registry.create()``
        까지 가지도 못하므로 여기 도달하지 않고, ``run_submitted``도
        ``run_failed``도 남기지 않는다.
        """
        existing = self.registry.get(run_id)
        if existing is not None:
            return BuildJobSubmitResult(status="existing", snapshot=existing)
        if self.registry.queued_count() >= self._max_queue_size:
            return BuildJobSubmitResult(status="queue_full")
        if on_accept is not None:
            on_accept()
        snapshot = self.registry.create(run_id=run_id, created_by=created_by, owner_id=owner_id)
        try:
            self._executor.submit(self._run, spec_yaml, run_id, created_by, runner)
        except Exception:
            self.registry.mark_failed(run_id, error="failed to queue build job")
            if on_enqueue_failure is not None:
                on_enqueue_failure()
            raise
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
    "AsyncBuildJobCounts",
    "AsyncBuildJobRegistry",
    "AsyncBuildStats",
    "BuildJobSubmitResult",
    "BuildJobResponse",
    "BuildJobSnapshot",
    "BuildJobStatus",
    "SubmitStatus",
    "generate_run_id",
]
