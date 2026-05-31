from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder import ArtifactDataset, ExportError
from kpubdata_builder.exporters import JsonlExporter, MarkdownExporter
from kpubdata_builder.spec import ExportTarget


def test_jsonl_exporter_raises_export_error_on_io_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = ArtifactDataset(records=({"id": "1"},), provenance=("datago.air_quality",))
    target = ExportTarget(kind="jsonl", output_path="out/data.jsonl")

    _orig_open = Path.open

    def raise_on_open(self: Path, *args: object, **kwargs: object) -> object:
        if "data.jsonl" in str(self):
            raise OSError("permission denied")
        return _orig_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", raise_on_open)

    with pytest.raises(ExportError):
        JsonlExporter().export(artifact, target, tmp_path)


def test_markdown_exporter_raises_export_error_on_io_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = ArtifactDataset(records=({"id": "1"},), provenance=("datago.air_quality",))
    target = ExportTarget(kind="markdown", output_path="out/README.md")

    def raise_io_error(self: Path, data: str, *, encoding: str) -> int:
        del self, data, encoding
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "write_text", raise_io_error)

    with pytest.raises(ExportError):
        MarkdownExporter().export(artifact, target, tmp_path)


def test_markdown_exporter_returns_export_metadata(tmp_path: Path) -> None:
    artifact = ArtifactDataset(records=({"id": "1"},), provenance=("datago.air_quality",))
    target = ExportTarget(kind="markdown", output_path="out/README.md")

    result = MarkdownExporter().export(artifact, target, tmp_path)

    assert result.output_path == tmp_path / "out/README.md"
    assert result.file_size > 0
    assert result.format == "markdown"
