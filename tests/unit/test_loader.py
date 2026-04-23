from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder import SpecLoadError
from kpubdata_builder.spec import load_spec


def test_load_spec_reads_valid_yaml(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
        + "\n",
        encoding="utf-8",
    )

    spec = load_spec(spec_path)

    assert spec.dataset_id == "dataset.sample"
    assert spec.sources[0].provider == "datago"
    assert spec.exports[0].output_path == "out/data.jsonl"


def test_load_spec_raises_for_invalid_yaml(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text("dataset_id: [unterminated\n", encoding="utf-8")

    with pytest.raises(SpecLoadError):
        load_spec(spec_path)


def test_load_spec_raises_for_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SpecLoadError):
        load_spec(tmp_path / "missing.yaml")
