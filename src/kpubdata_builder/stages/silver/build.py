"""Silver stage orchestration: BronzeArtifact -> SilverDataset."""

from __future__ import annotations

from collections.abc import Sequence

from ...tabular.polars_helpers import records_to_dataframe
from ..bronze.models import BronzeArtifact
from .models import SilverDataset
from .normalize import normalize_table
from .preview import DEFAULT_PREVIEW_ROWS, build_preview
from .summarize import summarize_schema, summarize_statistics
from .validate import validate_table


def build_silver_dataset(
    artifact: BronzeArtifact,
    *,
    transforms: Sequence[str] = (),
    preview_limit: int = DEFAULT_PREVIEW_ROWS,
) -> SilverDataset:
    """Tabularize a Bronze artifact and produce a validated Silver dataset.

    Steps: records -> DataFrame -> spec-declared normalization -> schema and
    statistics summaries -> preview slice -> structural validation.
    """
    table = records_to_dataframe(artifact.raw_records)
    table = normalize_table(table, transforms)

    schema_summary = summarize_schema(table)
    statistics = summarize_statistics(table)
    preview = build_preview(table, limit=preview_limit)
    validation_result = validate_table(table)

    return SilverDataset(
        table=table,
        schema_summary=schema_summary,
        statistics=statistics,
        preview=preview,
        validation_result=validation_result,
        source_bronze=artifact.source_key,
    )
