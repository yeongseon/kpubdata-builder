from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder import ArtifactDataset, ExportError
from kpubdata_builder.exporters import JsonlExporter, MarkdownExporter
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
