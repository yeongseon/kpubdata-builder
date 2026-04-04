"""Tests for spec models."""

from __future__ import annotations

from kpubdata_builder.spec import BuildSpec, ExportTarget, SourceRef


def test_build_spec_instantiation() -> None:
    """BuildSpec can be instantiated with minimum valid values."""
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=(SourceRef(provider="datago", dataset="air_quality"),),
        exports=(ExportTarget(kind="jsonl", output_path="out/data.jsonl"),),
    )

    assert spec.dataset_id == "dataset.sample"
