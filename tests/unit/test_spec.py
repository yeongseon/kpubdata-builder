"""Tests for spec models."""

from __future__ import annotations

from pathlib import Path

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


def test_from_yaml_emits_deprecation_warning(tmp_path: Path) -> None:
    import warnings

    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        "dataset_id: test\ntitle: T\ndescription: D\n"
        "sources:\n  - provider: p\n    dataset: d\n"
        "exports:\n  - kind: jsonl\n    output_path: o.jsonl\n",
        encoding="utf-8",
    )

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        spec = BuildSpec.from_yaml(spec_path)
        assert len(w) == 1
        assert issubclass(w[0].category, DeprecationWarning)
        assert "load_spec" in str(w[0].message)
    assert spec.dataset_id == "test"
