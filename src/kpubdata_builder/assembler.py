"""Assembly layer for combining source records into a canonical artifact.

.. deprecated::
    This module is part of the legacy package pipeline. The canonical pipeline
    is the Medallion pipeline in ``stages/`` and ``pipeline/``. This module
    will be removed in a future version.
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from dataclasses import dataclass

from .artifact import ArtifactDataset
from .errors import AssemblyError
from .spec import BuildSpec, JsonValue

_DEPRECATION_MSG = (
    "assemble_artifact() is part of the legacy pipeline. "
    "Use the Medallion pipeline (stages/bronze → silver → gold) instead."
)


@dataclass(frozen=True)
class AssemblyResult:
    artifact: ArtifactDataset
    warnings: tuple[str, ...] = ()


def assemble_artifact(
    spec: BuildSpec,
    records_by_source: dict[str, Sequence[dict[str, JsonValue]]],
) -> AssemblyResult:
    """Assemble source records with minimal deterministic merge behavior.

    .. deprecated::
        Use the Medallion pipeline instead.
    """
    warnings.warn(_DEPRECATION_MSG, DeprecationWarning, stacklevel=2)

    merged_records: list[dict[str, JsonValue]] = []
    warn_list: list[str] = []
    present_sources: list[str] = []
    for source in spec.sources:
        source_key = source.alias if source.alias else f"{source.provider}.{source.dataset}"
        source_records = records_by_source.get(source_key)
        if source_records is None:
            warn_list.append(f"Missing records for source: {source_key}")
            continue
        present_sources.append(source_key)
        merged_records.extend(source_records)

    if not present_sources:
        raise AssemblyError(f"No source records available for dataset {spec.dataset_id}")

    statistics = {"record_count": len(merged_records)}
    artifact = ArtifactDataset(
        records=tuple(merged_records),
        metadata=dict(spec.metadata),
        provenance=tuple(present_sources),
        statistics=statistics,
    )
    return AssemblyResult(artifact=artifact, warnings=tuple(warn_list))


__all__ = ["AssemblyResult", "assemble_artifact"]
