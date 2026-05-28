"""Persist Bronze stage artifacts to a run workspace."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ...spec import JsonValue
from .models import BronzeArtifact, ProvenanceEvent

_SAFE_PATH_SEGMENT = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


@dataclass(frozen=True)
class BronzePersistResult:
    """Filesystem paths written for a Bronze artifact."""

    bronze_dir: Path
    records_path: Path
    metadata_path: Path


def _validate_path_segment(value: str, *, field_name: str) -> None:
    """Reject path segments that could escape the workspace."""
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{field_name} must not have leading/trailing whitespace")
    if not _SAFE_PATH_SEGMENT.match(value):
        raise ValueError(
            f"{field_name} contains unsafe characters: {value!r}. "
            "Only alphanumeric, dot, hyphen, and underscore are allowed."
        )


def _artifact_id(artifact: BronzeArtifact) -> str:
    """Generate a short deterministic ID from source_key + fetch_params."""
    key_material = json.dumps(
        {"source_key": artifact.source_key, "fetch_params": artifact.fetch_params},
        sort_keys=True,
    )
    return hashlib.sha256(key_material.encode()).hexdigest()[:12]


def persist_bronze_artifact(
    artifact: BronzeArtifact,
    *,
    output_root: Path,
    run_id: str,
) -> BronzePersistResult:
    """Write raw records and metadata under build/{run_id}/bronze/{source_key}/{artifact_id}.

    Raises ValueError if run_id or source_key contain unsafe path characters.
    """
    _validate_path_segment(run_id, field_name="run_id")

    # Sanitize source_key for filesystem (e.g. "datago.apt_trade" → "datago.apt_trade")
    source_key_segment = artifact.source_key.replace("/", "_")
    _validate_path_segment(source_key_segment, field_name="source_key")

    artifact_id = _artifact_id(artifact)

    bronze_dir = output_root / run_id / "bronze" / source_key_segment / artifact_id
    records_path = bronze_dir / "raw_records.jsonl"
    metadata_path = bronze_dir / "metadata.json"

    # Final containment check: resolved path must be under output_root
    resolved_root = output_root.resolve()
    resolved_bronze = bronze_dir.resolve()
    if not str(resolved_bronze).startswith(str(resolved_root)):
        raise ValueError(
            f"Resolved bronze directory {resolved_bronze} escapes output_root {resolved_root}"
        )

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
        "record_count": provenance.record_count,
        "data_checksum": provenance.data_checksum,
    }


def _format_datetime(value: datetime) -> str:
    return value.isoformat()
