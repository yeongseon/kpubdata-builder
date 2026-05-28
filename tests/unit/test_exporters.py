from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder import ArtifactDataset, ExportError
from kpubdata_builder.exporters import CsvExporter, JsonlExporter, MarkdownExporter
from kpubdata_builder.spec import ExportTarget


@pytest.mark.parametrize(
    ("exporter", "target"),
    [
        (CsvExporter(), ExportTarget(kind="csv", output_path="out/data.csv")),
        (JsonlExporter(), ExportTarget(kind="jsonl", output_path="out/data.jsonl")),
        (MarkdownExporter(), ExportTarget(kind="markdown", output_path="out/README.md")),
    ],
)
def test_exporters_raise_export_error_on_bad_path(
    exporter: CsvExporter | JsonlExporter | MarkdownExporter,
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


def test_csv_exporter_writes_header_and_rows(tmp_path: Path) -> None:
    artifact = ArtifactDataset(
        records=({"id": "1", "name": "Alice"}, {"id": "2", "name": "Bob"}),
        provenance=("test",),
    )
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    assert result.output_path == tmp_path / "out/data.csv"
    assert result.file_size > 0
    assert result.format == "csv"

    lines = result.output_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "id,name"
    assert lines[1] == "1,Alice"
    assert lines[2] == "2,Bob"


def test_csv_exporter_empty_records(tmp_path: Path) -> None:
    artifact = ArtifactDataset(records=(), provenance=("test",))
    target = ExportTarget(kind="csv", output_path="out/empty.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    assert result.output_path.read_text(encoding="utf-8") == ""
