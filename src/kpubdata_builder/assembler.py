"""Assembly layer for combining source records into a canonical artifact."""

from __future__ import annotations

from collections.abc import Sequence

from .artifact import ArtifactDataset
from .spec import BuildSpec, JsonValue


def assemble_artifact(
    spec: BuildSpec,
    records_by_source: dict[str, Sequence[dict[str, JsonValue]]],
) -> ArtifactDataset:
    """Assemble source records with minimal deterministic merge behavior."""
    merged_records: list[dict[str, JsonValue]] = []
    for source in spec.sources:
        source_key = source.alias if source.alias else f"{source.provider}.{source.dataset}"
        merged_records.extend(records_by_source.get(source_key, ()))
    statistics = {"record_count": len(merged_records)}
    provenance = tuple(records_by_source.keys())
    return ArtifactDataset(
        records=tuple(merged_records),
        metadata=dict(spec.metadata),
        provenance=provenance,
        statistics=statistics,
    )
