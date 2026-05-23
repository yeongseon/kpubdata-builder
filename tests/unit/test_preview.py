"""Tests for the preview service (schema + sample records, no exports)."""

from __future__ import annotations

import pytest

from kpubdata_builder import ValidationError
from kpubdata_builder.artifact import ArtifactDataset
from kpubdata_builder.preview import (
    DEFAULT_PREVIEW_LIMIT,
    PreviewResult,
    build_preview_result,
    preview_build,
)
from kpubdata_builder.spec import BuildSpec, ExportTarget, SourceRef

_DEFAULT_SOURCES = (SourceRef(provider="datago", dataset="apt"),)


def _spec(sources: tuple[SourceRef, ...] = _DEFAULT_SOURCES) -> BuildSpec:
    return BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=sources,
        exports=(ExportTarget(kind="jsonl", output_path="out/data.jsonl"),),
    )


def test_build_preview_result_caps_samples_but_keeps_total() -> None:
    records = tuple({"id": str(i)} for i in range(12))
    artifact = ArtifactDataset(records=records)

    result = build_preview_result(artifact, limit=5)

    assert len(result.sample_records) == 5
    assert result.sample_records[0] == {"id": "0"}
    assert result.total_records == 12


def test_build_preview_result_default_limit_is_five() -> None:
    records = tuple({"id": str(i)} for i in range(10))
    result = build_preview_result(ArtifactDataset(records=records))

    assert DEFAULT_PREVIEW_LIMIT == 5
    assert len(result.sample_records) == 5


def test_build_preview_result_derives_schema_from_record_keys() -> None:
    artifact = ArtifactDataset(records=({"id": "1", "name": "a"}, {"id": "2", "amount": 3}))

    result = build_preview_result(artifact)

    assert result.schema == ("id", "name", "amount")


def test_build_preview_result_prefers_explicit_schema() -> None:
    artifact = ArtifactDataset(
        records=({"id": "1"},),
        schema={"id": "str", "amount": "int"},
    )

    result = build_preview_result(artifact)

    assert result.schema == ("id", "amount")


def test_build_preview_result_rejects_negative_limit() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        build_preview_result(ArtifactDataset(), limit=-1)


def test_preview_build_returns_preview_result_for_valid_spec() -> None:
    result = preview_build(_spec())

    assert isinstance(result, PreviewResult)
    assert result.total_records == 0
    assert result.sample_records == ()


def test_preview_build_raises_on_invalid_spec() -> None:
    with pytest.raises(ValidationError):
        preview_build(_spec(sources=()))
