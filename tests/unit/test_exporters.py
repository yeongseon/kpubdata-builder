from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder import ArtifactDataset, ExportError
from kpubdata_builder.exporters import JsonlExporter, KaggleExporter, MarkdownExporter
from kpubdata_builder.spec import ExportTarget


@pytest.mark.parametrize(
    ("exporter", "target"),
    [
        (JsonlExporter(), ExportTarget(kind="jsonl", output_path="out/data.jsonl")),
        (MarkdownExporter(), ExportTarget(kind="markdown", output_path="out/README.md")),
    ],
)
def test_exporters_raise_export_error_on_bad_path(
    exporter: JsonlExporter | MarkdownExporter,
    target: ExportTarget,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = ArtifactDataset(records=({"id": "1"},), provenance=("datago.air_quality",))

    def raise_io_error(self: Path, data: str, *, encoding: str) -> int:
        del self, data, encoding
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "write_text", raise_io_error)

    with pytest.raises(ExportError):
        exporter.export(artifact, target, tmp_path)


def test_markdown_exporter_returns_export_metadata(tmp_path: Path) -> None:
    artifact = ArtifactDataset(records=({"id": "1"},), provenance=("datago.air_quality",))
    target = ExportTarget(kind="markdown", output_path="out/README.md")

    result = MarkdownExporter().export(artifact, target, tmp_path)

    assert result.output_path == tmp_path / "out/README.md"
    assert result.file_size > 0
    assert result.format == "markdown"


def test_kaggle_exporter_creates_csv_and_metadata(tmp_path: Path) -> None:
    artifact = ArtifactDataset(
        records=({"id": "1", "name": "test"}, {"id": "2", "name": "test2"}),
        metadata={"dataset_id": "user/my-dataset", "title": "My Dataset"},
        provenance=("test",),
    )
    target = ExportTarget(kind="kaggle", output_path="kaggle_out/data.csv")

    result = KaggleExporter().export(artifact, target, tmp_path)

    assert result.format == "kaggle"
    assert result.output_path.exists()
    assert result.file_size > 0

    # Check CSV content
    lines = result.output_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "id,name"

    # Check metadata file
    import json

    meta_path = result.output_path.parent / "dataset-metadata.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["id"] == "user/my-dataset"
    assert meta["title"] == "My Dataset"


def test_kaggle_exporter_empty_records(tmp_path: Path) -> None:
    artifact = ArtifactDataset(records=(), provenance=("test",))
    target = ExportTarget(kind="kaggle", output_path="kaggle_out/data.csv")

    result = KaggleExporter().export(artifact, target, tmp_path)
    assert result.output_path.read_text(encoding="utf-8") == ""
