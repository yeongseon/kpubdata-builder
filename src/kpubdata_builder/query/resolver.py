"""Resolve a query context to one persisted table without exposing paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..service import datasets as datasets_service
from ..service import stages as stages_service
from ..service.auth import Principal
from ..stages._path_safety import ensure_within, validate_path_segment
from ..stages._stage_reader import gold_source_dir, silver_source_dir
from .models import QueryRequest


class QueryContextError(ValueError):
    pass


class QueryArtifactUnavailableError(QueryContextError):
    pass


@dataclass(frozen=True)
class ResolvedQueryContext:
    dataset_id: str
    run_id: str
    stage: str
    source: str
    table_path: Path


def _ownership_allowed(manifest: dict[str, object], principal: Principal) -> bool:
    if os.environ.get("ENFORCE_OWNERSHIP", "").lower() not in ("true", "1"):
        return True
    if principal.kind in ("dev", "service"):
        return True
    return manifest.get("created_by") == principal.label


def resolve_query_context(
    output_root: Path, request: QueryRequest, principal: Principal
) -> ResolvedQueryContext:
    try:
        validate_path_segment(request.run_id, field_name="run_id")
    except ValueError as exc:
        raise QueryContextError(str(exc)) from exc
    if request.stage not in ("silver", "gold"):
        raise QueryContextError("stage must be silver or gold")

    manifest = datasets_service.read_manifest(output_root, request.run_id)
    if manifest is None:
        raise QueryContextError("run not found")
    if not _ownership_allowed(manifest, principal):
        raise PermissionError("run is not owned by the principal")
    canonical_dataset_id = datasets_service.read_snapshot_dataset_id(output_root, request.run_id)
    if canonical_dataset_id is None:
        raise QueryContextError("canonical BuildSpec snapshot is unavailable")
    if canonical_dataset_id != request.dataset_id:
        raise QueryContextError("run does not belong to dataset_id")

    sources = stages_service.known_source_keys(manifest)
    if request.source is None:
        if len(sources) != 1:
            raise QueryContextError("source is required for a multi-source run")
        source = sources[0]
    else:
        source = request.source
        if source not in sources:
            raise QueryContextError("source is not part of the run")
    status = stages_service.stage_status_for_source(output_root, request.run_id, manifest, source)
    if status is None or stages_service.stage_status_of(status, request.stage) != "completed":
        raise QueryArtifactUnavailableError("requested stage artifact is unavailable")

    run_dir = output_root / request.run_id
    try:
        source_dir = (
            silver_source_dir(output_root, request.run_id, source)
            if request.stage == "silver"
            else gold_source_dir(output_root, request.run_id, source)
        )
    except ValueError as exc:
        raise QueryContextError(str(exc)) from exc
    table_path = source_dir / "table.parquet"
    ensure_within(run_dir, table_path, label="query table")
    if table_path.is_symlink() or not table_path.is_file():
        raise QueryArtifactUnavailableError("requested stage artifact is unavailable")
    return ResolvedQueryContext(
        dataset_id=canonical_dataset_id,
        run_id=request.run_id,
        stage=request.stage,
        source=source,
        table_path=table_path,
    )


__all__ = [
    "QueryArtifactUnavailableError",
    "QueryContextError",
    "ResolvedQueryContext",
    "resolve_query_context",
]
