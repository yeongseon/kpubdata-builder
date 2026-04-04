"""Tests for specification validation."""

from __future__ import annotations

from kpubdata_builder.spec import BuildSpec, ExportTarget, SourceRef
from kpubdata_builder.validator import validate_spec


def test_validate_spec_accepts_valid_spec() -> None:
    """validate_spec runs without raising for a valid build spec."""
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=(SourceRef(provider="datago", dataset="air_quality"),),
        exports=(ExportTarget(kind="markdown", output_path="README.md"),),
    )

    validate_spec(spec)
