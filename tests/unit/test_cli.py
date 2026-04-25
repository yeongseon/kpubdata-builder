from __future__ import annotations

from pathlib import Path

from kpubdata_builder.__main__ import main


def test_validate_command_accepts_valid_spec(tmp_path: Path, capsys) -> None:
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

    exit_code = main(["validate", str(spec_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "BuildSpec is valid." in captured.out
    assert "Dataset ID: dataset.sample" in captured.out
    assert "Title: Sample Dataset" in captured.out
    assert captured.err == ""


def test_validate_command_rejects_invalid_spec(tmp_path: Path, capsys) -> None:
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """
dataset_id: " "
title: Sample Dataset
description: Sample description
sources: []
exports: []
""".strip()
        + "\n",
        encoding="utf-8",
    )

    exit_code = main(["validate", str(spec_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "BuildSpec validation failed:" in captured.err
    assert "- dataset_id must be a non-empty string" in captured.err
    assert "- at least one source is required" in captured.err
    assert "- at least one export target is required" in captured.err


def test_validate_command_reports_load_errors(tmp_path: Path, capsys) -> None:
    exit_code = main(["validate", str(tmp_path / "missing.yaml")])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "BuildSpec load failed:" in captured.err
