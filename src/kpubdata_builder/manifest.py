"""Build manifest model and writer for reproducible build metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .errors import ManifestError


@dataclass(frozen=True)
class FieldSummary:
    """Schema description for a single field."""

    name: str
    type: str
    nullable: bool
    description: str = ""


@dataclass(frozen=True)
class SchemaSummary:
    """Aggregated schema information for an artifact."""

    fields: tuple[FieldSummary, ...]
    total_fields: int


def extract_schema_summary(
    records: tuple[dict[str, object], ...],
) -> SchemaSummary | None:
    """Derive a SchemaSummary from raw records.

    Returns ``None`` when *records* is empty.
    """
    if not records:
        return None

    all_keys: dict[str, None] = {}
    for rec in records:
        for k in rec:
            all_keys.setdefault(k, None)

    field_summaries: list[FieldSummary] = []
    for key in all_keys:
        types: set[str] = set()
        has_none = False
        for rec in records:
            val = rec.get(key)
            if val is None:
                has_none = True
            else:
                types.add(type(val).__name__)
        inferred_type = ", ".join(sorted(types)) if types else "unknown"
        field_summaries.append(FieldSummary(name=key, type=inferred_type, nullable=has_none))

    return SchemaSummary(
        fields=tuple(field_summaries),
        total_fields=len(field_summaries),
    )


@dataclass(frozen=True)
class BuildManifest:
    """Execution summary artifact for build auditing."""

    build_id: str
    started_at: datetime
    finished_at: datetime
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    row_counts: dict[str, int] = field(default_factory=dict)
    schema_summary: SchemaSummary | None = None


def _serialize_schema_summary(summary: SchemaSummary | None) -> dict[str, object] | None:
    if summary is None:
        return None
    return {
        "fields": [
            {"name": f.name, "type": f.type, "nullable": f.nullable, "description": f.description}
            for f in summary.fields
        ],
        "total_fields": summary.total_fields,
    }


def manifest_writer(manifest: BuildManifest, output_path: Path) -> None:
    """Write a build manifest as deterministic JSON to disk."""
    payload = {
        "build_id": manifest.build_id,
        "started_at": manifest.started_at.astimezone(timezone.utc).isoformat(),
        "finished_at": manifest.finished_at.astimezone(timezone.utc).isoformat(),
        "inputs": list(manifest.inputs),
        "outputs": list(manifest.outputs),
        "warnings": list(manifest.warnings),
        "errors": list(manifest.errors),
        "row_counts": manifest.row_counts,
        "schema_summary": _serialize_schema_summary(manifest.schema_summary),
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _ = output_path.write_text(f"{serialized}\n", encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"Failed to write manifest to {output_path}: {exc}") from exc


def write_manifest(manifest: BuildManifest, output_path: Path) -> None:
    """Write a build manifest as deterministic JSON to disk."""
    manifest_writer(manifest, output_path)


__all__ = [
    "BuildManifest",
    "FieldSummary",
    "SchemaSummary",
    "extract_schema_summary",
    "manifest_writer",
    "write_manifest",
]
