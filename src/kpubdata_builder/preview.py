"""Preview service: inspect a build's schema and sample records without exporting.

A preview is a "taste test" of a build — it runs validation and source execution
and surfaces the resulting schema plus a small sample of records, but never writes
any artifact files (README.md, .jsonl, .parquet, ...).
"""

from __future__ import annotations

from dataclasses import dataclass

from .artifact import ArtifactDataset
from .executor import source_executor
from .spec import BuildSpec, JsonValue

DEFAULT_PREVIEW_LIMIT = 5


@dataclass(frozen=True)
class PreviewResult:
    """Schema and a small record sample for inspecting a build before running it."""

    schema: tuple[str, ...] = ()
    sample_records: tuple[dict[str, JsonValue], ...] = ()
    total_records: int = 0
    provenance: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _derive_schema(artifact: ArtifactDataset) -> tuple[str, ...]:
    """Field names from the artifact schema, falling back to first-seen record keys."""
    if artifact.schema:
        return tuple(artifact.schema)
    seen: dict[str, None] = {}
    for record in artifact.records:
        for key in record:
            seen.setdefault(key, None)
    return tuple(seen)


def build_preview_result(
    artifact: ArtifactDataset, *, limit: int = DEFAULT_PREVIEW_LIMIT
) -> PreviewResult:
    """Derive a preview (schema + first ``limit`` records) from an assembled artifact.

    ``total_records`` always reflects the full record count regardless of ``limit``.
    """
    if limit < 0:
        raise ValueError("limit must be non-negative")
    return PreviewResult(
        schema=_derive_schema(artifact),
        sample_records=artifact.records[:limit],
        total_records=len(artifact.records),
        provenance=artifact.provenance,
    )


def preview_build(spec: BuildSpec, *, limit: int = DEFAULT_PREVIEW_LIMIT) -> PreviewResult:
    """Validate a spec, execute its sources, and return a preview without exporting.

    Validation is performed by :func:`source_executor`; an invalid spec raises
    ``ValidationError``. No artifact files are written.
    """
    execution = source_executor(spec)
    result = build_preview_result(execution.artifact, limit=limit)
    return PreviewResult(
        schema=result.schema,
        sample_records=result.sample_records,
        total_records=result.total_records,
        provenance=result.provenance,
        warnings=execution.warnings,
    )


__all__ = [
    "DEFAULT_PREVIEW_LIMIT",
    "PreviewResult",
    "build_preview_result",
    "preview_build",
]
