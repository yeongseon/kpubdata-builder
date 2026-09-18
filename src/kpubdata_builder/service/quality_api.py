"""Quality 도메인 서비스 (#596, 다섯 번째 조각).

run 단위 structured quality(#486/#514)와 최근 window 집계(#486 후속)를 담는다.

**datasets 도메인에 의존한다** — 24h 집계의 run 집합은 `DatasetsApiService` 의 canonical
record 수집을 그대로 쓴다. 앞 조각(#605)에서 그 헬퍼를 public 으로 둔 이유가 여기다:
같은 수집 로직을 복제하면 두 표면이 서로 다른 run 집합을 보게 되는 순간이 온다.

경계 하나를 지킨다 — **도메인 quality 와 시스템 observability(`/monitoring`)를 한 응답에
섞지 않는다.** 그래서 monitoring 은 이 서비스에 들어오지 않는다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from kpubdata_builder.service import datasets as datasets_service
from kpubdata_builder.service import quality as quality_service
from kpubdata_builder.service import stages as stages_service
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.datasets_api import DatasetsApiService
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.store.artifacts import ArtifactStore


class QualityApiService:
    """run별 structured quality 조회와 최근 window 집계 (#486/#514)."""

    def __init__(
        self,
        *,
        output_root: Path,
        store: ArtifactStore,
        datasets: DatasetsApiService,
    ) -> None:
        self._output_root = output_root
        self._store = store
        self._datasets = datasets

    def get_build_quality(self, run_id: str) -> ServiceResponse:
        """run의 구조화된 Quality 결과와 schema drift를 조회한다 (#486, #514).

        manifest.json에 이미 저장된 source_key별 quality_results/schema_drift를
        그대로 노출한다 — 별도 계산을 다시 하지 않는다(정본은 manifest).
        ``availability``/``evaluated_checks``는 빈 매핑이 "평가했지만 0건"인지
        "애초에 계산된 적이 없음"(legacy/partial run)인지 구분한다(#514).
        """
        manifest = self._store.get_manifest(run_id)
        if manifest is None:
            return ServiceResponse(404, {"error": f"manifest not found: {run_id}"})
        known_sources = stages_service.known_source_keys(manifest)
        availability, evaluated_checks = quality_service.quality_availability(
            manifest, known_sources
        )
        quality_results = manifest.get("quality_results")
        schema_drift = manifest.get("schema_drift")
        return ServiceResponse(
            200,
            {
                "run_id": run_id,
                "availability": availability,
                "evaluated_checks": evaluated_checks,
                "quality_results": cast(
                    JsonValue, quality_results if isinstance(quality_results, dict) else {}
                ),
                "schema_drift": cast(
                    JsonValue, schema_drift if isinstance(schema_drift, dict) else {}
                ),
            },
        )

    def quality_summary(
        self, *, window: str, principal: Principal | None = None
    ) -> ServiceResponse:
        """최근 ``window`` 안 접근 가능한 run의 structured quality를 PASS/WARN/FAIL
        run 수로 요약한다 (#486 후속, additive — API 1.22.0).

        개별 run의 ``quality_results``/dataset/owner는 노출하지 않는다 — 그건 per-run
        ``GET /builds/{run_id}/quality``의 몫이다. 시스템 observability(``/monitoring``)와
        도메인 quality를 한 응답에 섞지 않는다.

        run 집합은 datasets 도메인의 canonical record 수집을 재사용한다 — manifest mtime
        (+ 파생 BuildIndex 시간창)으로 candidate를 좁힌 뒤 canonical snapshot + manifest로
        재확인하며(ENFORCE_OWNERSHIP + oidc principal이면 본인 run만), all-history manifest
        재파싱은 하지 않는다. index는 파생물이라 단독으로 신뢰하지 않는다(ADR 0003).
        """
        if window != "24h":
            return ServiceResponse(400, {"error": f"unsupported window: {window!r} (only '24h')"})
        now = datetime.now(timezone.utc)
        base: dict[str, JsonValue] = {
            "window": "24h",
            "generated_at": now.isoformat(timespec="seconds"),
        }
        try:
            records = self._datasets.recent_canonical_records(
                principal,
                now=now,
                window_seconds=quality_service.QUALITY_SUMMARY_WINDOW_SECONDS,
            )
        except Exception:
            # run enumeration 자체가 불가능한 경우에만 unavailable — "0건"과 구분한다.
            return ServiceResponse(
                200,
                {
                    **base,
                    "availability": "unavailable",
                    "total_runs": 0,
                    "evaluated_runs": 0,
                    "pass_runs": 0,
                    "warn_runs": 0,
                    "fail_runs": 0,
                },
            )
        entries = (
            (record, datasets_service.read_manifest(self._output_root, record.run_id))
            for record in records
        )
        counts = quality_service.aggregate_quality_window(
            entries,
            now=now,
            window_seconds=quality_service.QUALITY_SUMMARY_WINDOW_SECONDS,
        )
        return ServiceResponse(200, {**base, "availability": "available", **counts})


__all__ = ["QualityApiService"]
