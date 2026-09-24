"""시스템 observability 서비스 (#596 후속, #637).

``/monitoring`` 표면이다 — 큐 상태, 최근 build 추이, 요청 지연.

**도메인 quality 와 섞지 않는다.** 그 경계는 #606 이 세운 것이고, 여기서도 같다:
`/quality` 는 데이터가 어떤가를 말하고 `/monitoring` 은 시스템이 어떤가를 말한다.
한 응답에 섞으면 둘 중 하나를 읽는 쪽이 다른 하나의 변화에 끌려다닌다.

**wire 계약은 바뀌지 않는다.** ``BuilderService`` 가 같은 시그니처로 위임한다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from kpubdata_builder.service import monitoring as monitoring_service
from kpubdata_builder.service import ownership as ownership_module
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.jobs import AsyncBuildExecutor
from kpubdata_builder.service.monitoring import LatencyRecorder
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.store.build_index import BuildIndex


class MonitoringApiService:
    """큐/build/지연 observability (#516)."""

    def __init__(
        self,
        *,
        output_root: Path,
        build_index: BuildIndex,
        async_builds: AsyncBuildExecutor,
        latency_recorder: LatencyRecorder,
    ) -> None:
        self._output_root = output_root
        self._build_index = build_index
        self._async_builds = async_builds
        self._latency_recorder = latency_recorder

    def monitoring_summary(self) -> ServiceResponse:
        """Builder API/Queue/Worker/Artifact Store 시스템 상태 요약 (#516).

        시스템 aggregate만 담으며 개별 run의 dataset/owner/credential 정보는
        포함하지 않는다 — ownership 필터링이 필요 없다. Provider status(#492)는
        요청마다 실제 네트워크 프로브를 유발하므로 이번 PR에서는 포함하지 않는다.
        """
        api = monitoring_service.api_status(self._latency_recorder)
        queue = monitoring_service.queue_status(self._async_builds)
        workers = monitoring_service.worker_status(self._async_builds)
        artifact_store = monitoring_service.artifact_store_status(
            self._output_root, self._build_index
        )
        status = monitoring_service.aggregate_status(
            api=api, queue=queue, workers=workers, artifact_store=artifact_store
        )
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return ServiceResponse(
            200,
            {
                "generated_at": generated_at,
                "status": status,
                "api": {
                    "availability": api.availability,
                    "sample_count": api.sample_count,
                    "p95_latency_ms": api.p95_latency_ms,
                },
                "queue": {
                    "availability": queue.availability,
                    "waiting": queue.waiting,
                    "running": queue.running,
                    "total": queue.total,
                },
                "workers": {
                    "availability": workers.availability,
                    "active": workers.active,
                    "capacity": workers.capacity,
                    "utilization": workers.utilization,
                },
                "artifact_store": {
                    "availability": artifact_store.availability,
                    "last_write_at": artifact_store.last_write_at,
                },
            },
        )

    def monitoring_builds(
        self, *, window: str, bucket: str, principal: Principal | None = None
    ) -> ServiceResponse:
        """window/bucket별 build 통계와 recent runs를 반환한다 (#516).

        ENFORCE_OWNERSHIP+oidc principal일 때는 본인이 접근 가능한 run만
        집계·노출한다(#505) — 다른 principal의 run metadata가 새는 side
        channel이 되지 않는다.
        """
        validated_window = monitoring_service.validate_window(window)
        if validated_window is None:
            return ServiceResponse(400, {"error": f"unsupported window: {window!r} (only '24h')"})
        validated_bucket = monitoring_service.validate_bucket(bucket)
        if validated_bucket is None:
            return ServiceResponse(400, {"error": f"unsupported bucket: {bucket!r} (only 'hour')"})

        stats = monitoring_service.build_statistics(
            self._build_index,
            window=validated_window,
            bucket=validated_bucket,
            principal=principal,
            enforce_ownership=ownership_module.enforce_ownership(),
        )
        buckets: list[JsonValue] = [
            {
                "bucket_start": b.bucket_start,
                "bucket_end": b.bucket_end,
                "total": b.total,
                # wire 계약은 success/failed/cancelled다(#527) — 내부 BuildIndex
                # status 값 "ok"는 그대로 두고 외부 필드 이름만 매핑한다.
                "success": b.success,
                "failed": b.failed,
                "cancelled": b.cancelled,
            }
            for b in stats.buckets
        ]
        recent_runs: list[JsonValue] = [
            {
                "run_id": r.run_id,
                "status": r.status,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
            }
            for r in stats.recent_runs
        ]
        return ServiceResponse(
            200,
            {
                "window": stats.window,
                "bucket": stats.bucket,
                "availability": stats.availability,
                "excluded_count": stats.excluded_count,
                "buckets": buckets,
                "recent_runs": recent_runs,
            },
        )
