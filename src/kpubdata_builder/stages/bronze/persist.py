"""Persist Bronze stage artifacts to a run workspace."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ...spec import JsonValue
from .models import BronzeArtifact, ProvenanceEvent


@dataclass(frozen=True)
class BronzePersistResult:
    """Filesystem paths written for a Bronze artifact."""

    bronze_dir: Path
    records_path: Path
    metadata_path: Path


def persist_bronze_artifact(
    artifact: BronzeArtifact,
    *,
    output_root: Path,
    run_id: str,
) -> BronzePersistResult:
    """Write raw records and metadata under build/{run_id}/bronze."""
    bronze_dir = output_root / run_id / "bronze"
    records_path = bronze_dir / "raw_records.jsonl"
    metadata_path = bronze_dir / "metadata.json"
    bronze_dir.mkdir(parents=True, exist_ok=True)

    records_content = "".join(
        f"{json.dumps(record, ensure_ascii=False, sort_keys=True)}\n"
        for record in artifact.raw_records
    )
    records_path.write_text(records_content, encoding="utf-8")

    metadata = _metadata_for_artifact(
        artifact,
        records_path=records_path,
        metadata_path=metadata_path,
        bronze_dir=bronze_dir,
    )
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return BronzePersistResult(
        bronze_dir=bronze_dir,
        records_path=records_path,
        metadata_path=metadata_path,
    )


def _metadata_for_artifact(
    artifact: BronzeArtifact,
    *,
    records_path: Path,
    metadata_path: Path,
    bronze_dir: Path,
) -> dict[str, JsonValue]:
    provenance = artifact.provenance
    return {
        "source_key": artifact.source_key,
        "fetch_params": artifact.fetch_params,
        "fetched_at": _format_datetime(artifact.fetched_at),
        "provenance": _provenance_to_dict(provenance) if provenance else None,
        "record_count": artifact.record_count,
        "artifact_paths": {
            "records": str(records_path.relative_to(bronze_dir)),
            "metadata": str(metadata_path.relative_to(bronze_dir)),
        },
    }


def _provenance_to_dict(provenance: ProvenanceEvent) -> dict[str, JsonValue]:
    return {
        "operation": provenance.operation,
        "source_key": provenance.source_key,
        "fetch_params": provenance.fetch_params,
        "fetched_at": _format_datetime(provenance.fetched_at),
    }


def _format_datetime(value: datetime) -> str:
    return value.isoformat()
