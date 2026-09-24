"""Dataset 도메인 서비스 (#596, 네 번째 조각).

``dataset_id`` 로 묶인 built dataset 의 조회 표면(#488/#486)을 담는다. 앞선 조각들과
같은 규칙이다 — **자기 의존성만 받고**, wire 계약은 건드리지 않는다.

이 도메인은 **run record 수집 헬퍼를 quality 도메인과 공유한다**. 그래서 헬퍼를
private 으로 숨기지 않고 공개 메서드로 둔다 — quality 조각이 옮겨올 때
``DatasetsApiService`` 를 의존성으로 받아 쓰면 되고, 같은 수집 로직이 두 벌로
복제되지 않는다.

ownership 필터를 **grouping/latest 선정보다 먼저** 적용하는 규칙(#488 semantics D)이
이 파일의 핵심이다. 순서가 뒤집히면 같은 ``dataset_id`` 를 쓰는 타 사용자의 run 이
latest 후보에 섞여 들어간다.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

from kpubdata_builder.service import datasets as datasets_service
from kpubdata_builder.service import quality as quality_service
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.store.artifacts import ArtifactStore
from kpubdata_builder.store.build_index import BuildIndex

# BuildIndex 의 window 조회를 보강하는 mtime 여유분. mtime 은 완료 시점에 만들어지는
# 정본 파일의 것이라 항상 finished_at 이상이지만, 완료 후 재기록(예: secret
# redaction)·시계 오차·파일시스템 mtime 해상도를 흡수하려 window 하한을 이만큼 더
# 내려 잡는다. 정확한 경계는 quality.aggregate_quality_window 가 canonical timestamp
# 로 다시 적용한다.
QUALITY_WINDOW_MTIME_MARGIN_SECONDS = 3600


class DatasetsApiService:
    """built dataset 목록·상세·run history·quality 이력 (#488/#486)."""

    def __init__(
        self,
        *,
        output_root: Path,
        build_index: BuildIndex,
        store: ArtifactStore,
        enforce_ownership: bool | None = None,
    ) -> None:
        self._output_root = output_root
        self._build_index = build_index
        self._store = store
        # None 이면 호출 시점에 환경을 다시 읽는다 — 테스트가 env 로 토글하는 방식을
        # 그대로 유지한다(#389).
        self._enforce_ownership_override = enforce_ownership

    def _enforce_ownership(self) -> bool:
        if self._enforce_ownership_override is not None:
            return self._enforce_ownership_override
        from kpubdata_builder.service import ownership as ownership_module

        return ownership_module.enforce_ownership()

    # --- run record 수집 (quality 도메인과 공유) ----------------------------

    def dataset_records(self, principal: Principal | None) -> list[datasets_service.RunRecord]:
        """dataset_id가 있는 접근 가능한 모든 run을 얻는다 (인덱스 우선, 파일시스템 폴백).

        ownership 필터를 grouping/latest 선정보다 먼저 적용한다 — 동일 dataset_id의
        타 사용자 run이 latest 후보에 섞이지 않게 한다 (#488 semantics D).
        """
        index_records = datasets_service.collect_run_records_from_index(self._build_index) or []
        filesystem_records = datasets_service.collect_run_records_from_filesystem(self._output_root)
        records = datasets_service.merge_run_records(index_records, filesystem_records)
        records = datasets_service.retain_canonical_run_records(self._output_root, records)
        return datasets_service.filter_ownership(
            records, principal, enforce=self._enforce_ownership()
        )

    def dataset_records_for(
        self, dataset_id: str, principal: Principal | None
    ) -> list[datasets_service.RunRecord]:
        """특정 dataset_id의 접근 가능한 canonical run을 모두 얻는다."""
        return [
            record for record in self.dataset_records(principal) if record.dataset_id == dataset_id
        ]

    def recent_canonical_records(
        self, principal: Principal | None, *, now: datetime, window_seconds: int
    ) -> list[datasets_service.RunRecord]:
        """최근 ``window_seconds`` 안 candidate run을 canonical 정본으로 확정한다 (#488 후속 리뷰).

        candidate run_id는 두 신호의 합집합이다:
          - canonical ``manifest.json``의 mtime이 window 안(margin 포함)인 run.
          - ``BuildIndex.list_between``의 window 안 run (파생 index fast lookup).

        BuildIndex는 파생 검색 index이고 write는 best-effort다 (ADR 0003) — 누락되거나
        stale한 row가 정본 24h aggregate를 바꾸면 안 되므로, index 단독으로 좁히지 않고
        위 mtime 후보로 보강한다. index 조회가 실패해도 mtime 후보가 전체를 커버한다.
        """
        margin_seconds = window_seconds + QUALITY_WINDOW_MTIME_MARGIN_SECONDS
        mtime_cutoff = now.timestamp() - margin_seconds
        candidate_run_ids: set[str] = set()
        if self._output_root.exists():
            for run_dir in self._output_root.iterdir():
                if not run_dir.is_dir():
                    continue
                try:
                    manifest_mtime = (run_dir / "manifest.json").stat().st_mtime
                except OSError:
                    continue  # manifest 부재/접근 불가 — 완료된 run 아님
                if manifest_mtime >= mtime_cutoff:
                    candidate_run_ids.add(run_dir.name)
        lower = (now - timedelta(seconds=margin_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        upper = (now + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # 파생 index 조회가 실패해도 mtime 후보가 이미 전체 run을 커버한다.
        with suppress(Exception):
            candidate_run_ids.update(
                entry.run_id for entry in self._build_index.list_between(lower, upper)
            )
        canonical = datasets_service.canonical_records_for_run_ids(
            self._output_root, candidate_run_ids
        )
        return datasets_service.filter_ownership(
            canonical, principal, enforce=self._enforce_ownership()
        )

    # --- 공개 엔드포인트 ---------------------------------------------------

    def list_datasets(
        self, *, limit: int = 50, principal: Principal | None = None
    ) -> ServiceResponse:
        """동일 dataset_id의 여러 run을 하나의 built dataset으로 묶어 목록을 반환한다 (#488).

        `total`은 canonical grouping + ownership 필터 이후, pagination(limit) 이전의
        distinct dataset 개수다. expensive full summary 는 응답 page 후보에만 수행하고,
        page를 채운 뒤로는 경량 renderability 검증만 한다 — limit=1 + 대량 dataset에서
        catalog 전체에 full summary가 도는 regression 을 막는다(#488 후속 리뷰).
        """
        records = self.dataset_records(principal)
        latest_by_dataset = datasets_service.group_latest_by_dataset(records)
        ordered = sorted(
            latest_by_dataset.values(),
            key=datasets_service.sort_key,
            reverse=True,
        )
        items: list[JsonValue] = []
        total = 0
        for record in ordered:
            if len(items) < limit:
                view = datasets_service.build_dataset_summary(self._output_root, record)
                if view is None:
                    continue
                items.append(view)
                total += 1
            elif datasets_service.dataset_summary_renderable(self._output_root, record):
                total += 1
        return ServiceResponse(200, {"datasets": items, "total": total})

    def get_dataset(
        self, dataset_id: str, *, principal: Principal | None = None
    ) -> ServiceResponse:
        """단일 built dataset의 canonical 요약을 반환한다 (#488).

        접근 가능한 run이 하나도 없으면(dataset_id가 실제로 없거나, 있어도 전부
        타 사용자 소유이면) 404 — 어느 경우인지는 구분해 노출하지 않는다.
        """
        records = self.dataset_records_for(dataset_id, principal)
        if not records:
            return ServiceResponse(404, {"error": f"dataset not found: {dataset_id}"})
        latest = datasets_service.pick_latest(records)
        view = datasets_service.build_dataset_summary(self._output_root, latest)
        if view is None:
            return ServiceResponse(404, {"error": f"dataset not found: {dataset_id}"})
        view["run_count"] = len(records)
        return ServiceResponse(200, view)

    def list_dataset_runs(
        self, dataset_id: str, *, limit: int = 50, principal: Principal | None = None
    ) -> ServiceResponse:
        """dataset_id의 접근 가능한 run history를 최신순으로 반환한다 (#488)."""
        records = self.dataset_records_for(dataset_id, principal)
        if not records:
            return ServiceResponse(404, {"error": f"dataset not found: {dataset_id}"})
        ordered = sorted(records, key=datasets_service.sort_key, reverse=True)[:limit]
        runs: list[JsonValue] = [
            {
                "run_id": r.run_id,
                "status": r.status,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "spec_digest": r.spec_digest,
                "created_by": r.created_by,
            }
            for r in ordered
        ]
        return ServiceResponse(200, {"dataset_id": dataset_id, "runs": runs})

    def get_dataset_quality_history(
        self, dataset_id: str, *, limit: int = 30, principal: Principal | None = None
    ) -> ServiceResponse:
        """dataset_id의 접근 가능한 run들에 대한 quality PASS/WARN/FAIL 집계 이력 (#486).

        dataset→run 조회는 ``dataset_records_for``(ownership 포함)를 재사용한다 — 새
        grouping/index 를 만들지 않는다. 존재/ownership 판정은 다른 dataset 엔드포인트와
        같다.
        """
        records = self.dataset_records_for(dataset_id, principal)
        if not records:
            return ServiceResponse(404, {"error": f"dataset not found: {dataset_id}"})
        ordered = sorted(records, key=datasets_service.sort_key, reverse=True)[:limit]
        runs: list[JsonValue] = []
        for r in ordered:
            manifest = self._store.get_manifest(r.run_id) or {}
            runs.append(cast(JsonValue, quality_service.summarize_run_quality(r, manifest)))
        return ServiceResponse(200, {"dataset_id": dataset_id, "runs": runs})


__all__ = ["DatasetsApiService", "QUALITY_WINDOW_MTIME_MARGIN_SECONDS"]
