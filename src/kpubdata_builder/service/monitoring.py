"""System Resource·Build Statistics 조회 로직 (#516).

Studio Monitoring 화면에 필요한 Builder API/Queue/Worker/Artifact Store 상태와
BuildIndex 기반 시간대별 build 통계를 제공한다. Run 단위 이벤트는 #496이
담당하며 이 모듈은 시스템/집계 observability만 다룬다.

핵심 원칙("없는 상태를 만들어내지 말 것"):
    - 측정된 적 없는 값은 0/healthy 등으로 위장하지 않고 ``null``/``unavailable``로
      표현한다.
    - ``Availability`` vocabulary는 ``quality.py``가 이미 정의한 것을 그대로
      재사용한다(``available``/``partial``/``unavailable``).

Async build 실행 모델(queued/running worker pool)은 ``jobs.AsyncBuildExecutor``/
``AsyncBuildJobRegistry``(#511/#513)로 이미 구현되어 있고 ``BuilderService``가
항상 생성해 사용한다 — queue/worker 상태는 그 실행기의 read-only snapshot을
그대로 반영한다. 존재하지 않는 실행기를 흉내 내 값을 위장하지도, 실행기가
없는 구성을 새로 만들지도 않는다.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from ..store import BuildEntry, BuildIndex
from .auth import Principal, principal_owns
from .jobs import AsyncBuildExecutor
from .quality import Availability

# Latency 표본은 시간창이 아니라 최근 최대 N개 요청의 고정 크기 ring buffer다
# (#516) — 메모리 상한이 목적이며 "지난 X분" 같은 시간 기반 window가 아니다.
_LATENCY_WINDOW_SIZE = 1000

# recent runs는 Monitoring 카드용 미리보기이므로 /builds처럼 클라이언트가
# limit을 조정하게 하지 않고 작은 고정 개수만 노출한다 (#516).
_RECENT_RUNS_LIMIT = 10

BuildBucketWindow = Literal["24h"]
BuildBucketGranularity = Literal["hour"]

_SUPPORTED_WINDOWS: dict[str, int] = {"24h": 24 * 3600}
_SUPPORTED_BUCKETS: dict[str, int] = {"hour": 3600}


class LatencyRecorder:
    """Bounded, thread-safe in-memory Builder API 요청 latency 기록기 (#516).

    ``dispatch()`` 전체 실행 시간(라우팅+인증+비즈니스 로직)을 ms 단위로
    기록한다. 실제 HTTP 소켓 I/O는 포함하지 않는다(그건 http.py 계층).
    """

    def __init__(self, *, max_samples: int = _LATENCY_WINDOW_SIZE) -> None:
        self._samples: deque[float] = deque(maxlen=max_samples)
        self._lock = threading.Lock()

    def record(self, latency_ms: float) -> None:
        """표본을 기록한다. 실패해도 예외를 전파하지 않는다(#516 요구사항)."""
        try:
            with self._lock:
                self._samples.append(latency_ms)
        except Exception:
            # metric collection 실패가 요청 실패로 전파되면 안 된다 (#516).
            pass

    def snapshot(self) -> tuple[int, float | None]:
        """``(sample_count, p95_latency_ms)``를 반환한다. 표본이 없으면 p95는 None."""
        try:
            with self._lock:
                samples = list(self._samples)
        except Exception:
            return 0, None
        return _p95(samples)


def _p95(samples: list[float]) -> tuple[int, float | None]:
    """nearest-rank 방식으로 p95를 계산한다 (#516, 보간 없음).

    ``rank = clamp(ceil(0.95 * n), 1, n)`` 을 오름차순 정렬된 표본의 1-indexed
    순위로 사용한다. 예: n=1 -> rank=1(그 표본 자체), n=20 -> rank=19.
    """
    n = len(samples)
    if n == 0:
        return 0, None
    ordered = sorted(samples)
    rank = max(1, min(math.ceil(0.95 * n), n))
    return n, ordered[rank - 1]


@dataclass(frozen=True)
class ApiStatus:
    availability: Availability
    sample_count: int
    p95_latency_ms: float | None


def api_status(recorder: LatencyRecorder) -> ApiStatus:
    """Builder API 상태를 반환한다.

    이 함수가 호출된다는 사실 자체가 프로세스가 응답 중임을 증명하므로
    availability는 항상 ``available``이다. Healthy/Degraded 같은 latency
    임계값 판정은 근거(ADR/config)가 없어 이번 PR에서 발명하지 않는다 — raw
    ``sample_count``/``p95_latency_ms``만 제공한다.
    """
    sample_count, p95 = recorder.snapshot()
    return ApiStatus(availability="available", sample_count=sample_count, p95_latency_ms=p95)


@dataclass(frozen=True)
class QueueStatus:
    availability: Availability
    waiting: int | None
    running: int | None
    total: int | None


@dataclass(frozen=True)
class WorkerStatus:
    availability: Availability
    active: int | None
    capacity: int | None
    utilization: float | None


def queue_status(async_builds: AsyncBuildExecutor) -> QueueStatus:
    """Async build queue 상태 (#516).

    ``BuilderService``가 항상 생성하는 ``AsyncBuildExecutor``(#511/#513)의
    read-only snapshot(``stats()``)을 그대로 반영한다 — 이 서비스에서 async
    build는 항상 지원되므로 availability는 항상 ``available``이다.
    ``total``은 ``waiting + running``이다(terminal succeeded/failed/cancelled
    history는 workload 상태가 아니므로 섞지 않는다 — registry가 이들을 메모리에
    계속 보존하더라도 ``stats()``가 이미 제외한다).
    """
    stats = async_builds.stats()
    return QueueStatus(
        availability="available",
        waiting=stats.queued,
        running=stats.running,
        total=stats.queued + stats.running,
    )


def worker_status(async_builds: AsyncBuildExecutor) -> WorkerStatus:
    """Async build worker pool 상태. 근거는 ``queue_status`` 문서 참조 (#516).

    ``active``는 현재 running job 수(워커 하나가 job 하나를 실행하므로
    running == active), ``capacity``는 실행기 생성 시 보존한 ``max_workers``다.
    ``ThreadPoolExecutor``의 private field는 직접 읽지 않는다 —
    ``AsyncBuildExecutor.stats()``가 이미 capacity를 노출한다.
    """
    stats = async_builds.stats()
    utilization = (stats.running / stats.capacity) if stats.capacity > 0 else 0.0
    return WorkerStatus(
        availability="available",
        active=stats.running,
        capacity=stats.capacity,
        utilization=utilization,
    )


@dataclass(frozen=True)
class ArtifactStoreStatus:
    availability: Availability
    last_write_at: str | None


def artifact_store_status(output_root: Path, build_index: BuildIndex) -> ArtifactStoreStatus:
    """Artifact Store 상태 (#516).

    폴더 존재만으로 healthy로 간주하지 않는다 — ``output_root``가 디렉터리로
    접근 가능하고 BuildIndex 쿼리도 성공해야 ``available``이다.
    ``last_write_at``은 BuildIndex에 실제로 기록된 가장 최근 성공(``ok``) 빌드의
    ``finished_at``에서만 얻는다(성공 기록이 없으면 ``available``이되 ``null``
    — "0건"과 "확인 불가"를 구분).
    """
    if not output_root.exists() or not output_root.is_dir():
        return ArtifactStoreStatus(availability="unavailable", last_write_at=None)
    try:
        last_write_at = build_index.latest_successful_finished_at()
    except Exception:
        return ArtifactStoreStatus(availability="unavailable", last_write_at=None)
    return ArtifactStoreStatus(availability="available", last_write_at=last_write_at)


MonitoringAggregateStatus = Literal["healthy", "degraded"]


def aggregate_status(
    *,
    api: ApiStatus,
    queue: QueueStatus,
    workers: WorkerStatus,
    artifact_store: ArtifactStoreStatus,
) -> MonitoringAggregateStatus:
    """Required subsystem availability로부터 deterministic aggregate status를 판정한다 (#516).

    latency threshold(SLA)는 근거(ADR/config)가 없어 사용하지 않는다 —
    ``sample_count=0``/``p95_latency_ms=None``은 startup/무표본 상태일 수 있으므로
    그 자체로는 degraded 근거가 아니다(``api.availability``만 본다). Provider
    status는 #516에서 optional이라 이 판정에 포함하지 않는다(호출자가 아예
    전달하지 않는다).

    required subsystem(api/queue/workers/artifact_store) availability가 모두
    ``available``이면 ``healthy``, 하나라도 ``partial``/``unavailable``이면
    ``degraded``. queue/workers의 실제 0(waiting/running/active=0)은
    availability와 무관한 값이므로 이 판정에 영향을 주지 않는다 — availability
    자체가 unavailable일 때만 degraded로 반영된다.
    """
    required_availabilities = (
        api.availability,
        queue.availability,
        workers.availability,
        artifact_store.availability,
    )
    if all(availability == "available" for availability in required_availabilities):
        return "healthy"
    return "degraded"


@dataclass(frozen=True)
class BuildBucket:
    bucket_start: str
    bucket_end: str
    total: int
    ok: int
    failed: int
    cancelled: int


@dataclass(frozen=True)
class RecentRun:
    run_id: str
    status: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True)
class BuildStatistics:
    window: str
    bucket: str
    availability: Availability
    excluded_count: int
    buckets: tuple[BuildBucket, ...]
    recent_runs: tuple[RecentRun, ...]


def validate_window(window: str) -> BuildBucketWindow | None:
    """지원하는 window 값이면 그대로, 아니면 None (#516 — 범위를 넓히지 않음)."""
    return "24h" if window == "24h" else None


def validate_bucket(bucket: str) -> BuildBucketGranularity | None:
    """지원하는 bucket 값이면 그대로, 아니면 None (#516 — 범위를 넓히지 않음)."""
    return "hour" if bucket == "hour" else None


def _parse_iso_utc(value: str | None) -> datetime | None:
    """ISO 8601 문자열을 UTC ``datetime``으로 파싱한다. 실패/None이면 None.

    malformed/legacy timestamp를 조용히 포함시키지 않기 위한 엄격한 검증 —
    파싱 실패 행은 호출자가 ``excluded_count``로 집계해 partial 판정에 반영한다.
    """
    if not value:
        return None
    try:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bucket_start(dt: datetime, bucket_seconds: int) -> datetime:
    epoch_seconds = int(dt.timestamp())
    floored = (epoch_seconds // bucket_seconds) * bucket_seconds
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def _isoformat_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _filter_ownership(
    entries: list[BuildEntry], principal: Principal | None, *, enforce: bool
) -> list[BuildEntry]:
    """BuildEntry 목록에 기존 ownership 정책을 적용한다 (#516, #505).

    ``app._apply_ownership``/``datasets.filter_ownership``과 동일한 정책 —
    ENFORCE_OWNERSHIP+oidc principal일 때만 필터링하며, dev/service/None은
    관리자 권한으로 통과한다. Monitoring 집계·recent runs가 다른 principal의
    run metadata를 노출하는 side channel이 되지 않도록 한다.
    """
    if not (enforce and principal is not None and principal.kind == "oidc"):
        return entries
    return [
        e
        for e in entries
        if principal_owns(created_by=e.created_by, owner_id=e.owner_id, principal=principal)
    ]


def build_statistics(
    build_index: BuildIndex,
    *,
    window: BuildBucketWindow,
    bucket: BuildBucketGranularity,
    principal: Principal | None,
    enforce_ownership: bool,
    now: datetime | None = None,
) -> BuildStatistics:
    """window/bucket에 따른 build 통계와 recent runs를 집계한다 (#516).

    timezone: UTC. bucket 경계는 ``[start, end)`` 반열린. bucket 기준
    timestamp는 ``finished_at``(BuildIndex는 완료된 빌드만 기록, ADR 0003).

    - BuildIndex 쿼리 자체가 실패하면 ``unavailable`` (buckets 비어있음).
    - 쿼리는 성공했지만 malformed timestamp로 일부 행이 제외되면 ``partial``.
    - 쿼리 성공 + 제외 없음이면 ``available`` (0건이어도 유효한 available).
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    window_seconds = _SUPPORTED_WINDOWS[window]
    bucket_seconds = _SUPPORTED_BUCKETS[bucket]
    window_end = _bucket_start(current, bucket_seconds) + timedelta(seconds=bucket_seconds)
    window_start = window_end - timedelta(seconds=window_seconds)

    try:
        raw_entries = build_index.list_between(_isoformat_z(window_start), _isoformat_z(window_end))
        recent_entries = build_index.list_builds(limit=_RECENT_RUNS_LIMIT)
    except Exception:
        return BuildStatistics(
            window=window,
            bucket=bucket,
            availability="unavailable",
            excluded_count=0,
            buckets=(),
            recent_runs=(),
        )

    scoped_entries = _filter_ownership(raw_entries, principal, enforce=enforce_ownership)
    scoped_recent = _filter_ownership(recent_entries, principal, enforce=enforce_ownership)

    bucket_count = window_seconds // bucket_seconds
    bucket_starts = [
        window_start + timedelta(seconds=i * bucket_seconds) for i in range(bucket_count)
    ]
    counters: dict[str, dict[str, int]] = {
        _isoformat_z(start): {"total": 0, "ok": 0, "failed": 0, "cancelled": 0}
        for start in bucket_starts
    }

    excluded_count = 0
    for entry in scoped_entries:
        parsed = _parse_iso_utc(entry.finished_at)
        if parsed is None:
            excluded_count += 1
            continue
        key = _isoformat_z(_bucket_start(parsed, bucket_seconds))
        counter = counters.get(key)
        if counter is None:
            # window 쿼리 자체가 [start,end)로 한정하므로 정상적으로는 발생하지
            # 않지만, 경계 근처 부동소수/파싱 편차에 대비한 방어적 처리.
            excluded_count += 1
            continue
        counter["total"] += 1
        if entry.status in ("ok", "failed", "cancelled"):
            counter[entry.status] += 1

    buckets = tuple(
        BuildBucket(
            bucket_start=_isoformat_z(start),
            bucket_end=_isoformat_z(start + timedelta(seconds=bucket_seconds)),
            total=counters[_isoformat_z(start)]["total"],
            ok=counters[_isoformat_z(start)]["ok"],
            failed=counters[_isoformat_z(start)]["failed"],
            cancelled=counters[_isoformat_z(start)]["cancelled"],
        )
        for start in bucket_starts
    )
    recent_runs = tuple(
        RecentRun(
            run_id=e.run_id, status=e.status, started_at=e.started_at, finished_at=e.finished_at
        )
        for e in scoped_recent
    )

    availability: Availability = "partial" if excluded_count > 0 else "available"
    return BuildStatistics(
        window=window,
        bucket=bucket,
        availability=availability,
        excluded_count=excluded_count,
        buckets=buckets,
        recent_runs=recent_runs,
    )


__all__ = [
    "ApiStatus",
    "ArtifactStoreStatus",
    "BuildBucket",
    "BuildStatistics",
    "LatencyRecorder",
    "MonitoringAggregateStatus",
    "QueueStatus",
    "RecentRun",
    "WorkerStatus",
    "aggregate_status",
    "api_status",
    "artifact_store_status",
    "build_statistics",
    "queue_status",
    "validate_bucket",
    "validate_window",
    "worker_status",
]
