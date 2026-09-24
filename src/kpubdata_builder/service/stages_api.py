"""Run stage 조회 서비스 (#596 후속, #637).

``/builds/{run_id}/stages`` 와 단계별 상세(Bronze/Silver/Gold)를 담는다.

읽기 전용이고 필요한 것은 ``output_root`` 와 artifact store 둘뿐이다 — 도메인
경계가 가장 선명한 조각이라 먼저 뗀다.

**wire 계약은 바뀌지 않는다.** ``BuilderService`` 가 같은 시그니처로 위임한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from kpubdata_builder.service import datasets as datasets_service
from kpubdata_builder.service import stages as stages_service
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.store.artifacts import ArtifactStore


class StagesApiService:
    """run 의 단계별 산출물 조회 (#488)."""

    def __init__(self, *, output_root: Path, store: ArtifactStore) -> None:
        self._output_root = output_root
        self._store = store

    def list_run_stages(self, run_id: str) -> ServiceResponse:
        """run에 알려진 모든 source의 Bronze/Silver/Gold 상태를 반환한다 (#488).

        호출 전에 run_id 검증·존재 확인·ownership 게이팅이 끝나 있어야 한다
        (dispatch가 다른 /builds/{run_id}/* 라우트와 동일한 순서로 처리한다).
        """
        manifest = self._store.get_manifest(run_id)
        if manifest is None:
            return ServiceResponse(404, {"error": f"manifest not found: {run_id}"})
        summaries = stages_service.list_run_stages(self._output_root, run_id, manifest)
        sources: list[JsonValue] = [
            {
                "source_key": s.source_key,
                "bronze": {"status": s.bronze, "available": s.bronze == "completed"},
                "silver": {"status": s.silver, "available": s.silver == "completed"},
                "gold": {"status": s.gold, "available": s.gold == "completed"},
            }
            for s in summaries
        ]
        return ServiceResponse(200, {"run_id": run_id, "sources": sources})

    def get_run_stage_detail(
        self, run_id: str, stage: str, source_key: str, *, limit: int
    ) -> ServiceResponse:
        """단일 source의 단일 stage에 대한 안전한 summary/preview를 반환한다 (#488).

        순서: stage 이름 검증(구조) → manifest에서 known source 확인 → 각 stage
        reader가 sidecar만 읽어 응답을 구성한다. raw fetch_params/export
        options/credential/absolute path는 어디에도 담지 않는다.
        """
        if stage not in stages_service.STAGE_NAMES:
            return ServiceResponse(
                400, {"error": f"invalid stage: {stage!r}; must be one of bronze/silver/gold"}
            )
        manifest = self._store.get_manifest(run_id)
        if manifest is None:
            return ServiceResponse(404, {"error": f"manifest not found: {run_id}"})
        summary = stages_service.stage_status_for_source(
            self._output_root, run_id, manifest, source_key
        )
        if summary is None:
            return ServiceResponse(404, {"error": f"unknown source: {source_key}"})

        status = stages_service.stage_status_of(summary, stage)
        body: dict[str, JsonValue] = {
            "run_id": run_id,
            "stage": stage,
            "source_key": source_key,
            "status": status,
            "available": status == "completed",
        }

        if stage == "bronze":
            bronze = stages_service.bronze_detail(self._output_root, run_id, source_key)
            spec = datasets_service.read_snapshot_spec(self._output_root, run_id)
            matched = stages_service.match_source_ref(spec, source_key) if spec else None
            body["provider"] = matched.provider if matched is not None else None
            body["dataset"] = matched.dataset if matched is not None else None
            body["fetched_at"] = bronze.fetched_at if bronze is not None else None
            body["record_count"] = bronze.record_count if bronze is not None else None
        elif stage == "silver":
            silver = stages_service.silver_detail(
                self._output_root, run_id, source_key, limit=limit
            )
            body["row_count"] = silver.row_count if silver is not None else None
            body["schema"] = silver.schema if silver is not None else []
            body["statistics"] = silver.statistics if silver is not None else None
            body["validation"] = silver.validation if silver is not None else None
            body["sample"] = silver.sample if silver is not None else []
        else:  # gold
            gold = stages_service.gold_detail(self._output_root, run_id, source_key)
            body["row_count"] = gold.row_count if gold is not None else None
            body["columns"] = cast(JsonValue, gold.columns) if gold is not None else []
            body["splits"] = cast(JsonValue, gold.splits) if gold is not None else None
            body["exports"] = (
                cast(JsonValue, [{"kind": kind} for kind in gold.export_kinds])
                if gold is not None
                else []
            )
            # Gold sample sidecar가 아직 없으므로 만들어내지 않는다 — Silver sample을
            # 가장하지 않고 명시적으로 unavailable을 표현한다.
            body["sample"] = None
            body["sample_available"] = False

        return ServiceResponse(200, body)
