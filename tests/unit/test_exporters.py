"""기본 exporter 구현의 오류 래핑과 결과 메타데이터를 검증한다."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kpubdata_builder import ArtifactDataset, ExportError
from kpubdata_builder.errors import PathTraversalError
from kpubdata_builder.exporters import JsonlExporter, MarkdownExporter
from kpubdata_builder.spec import ExportTarget


def test_jsonl_exporter_raises_export_error_on_io_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 파일 쓰기 실패가 ExportError로 일관되게 래핑되는지 확인한다.
    artifact = ArtifactDataset(records=({"id": "1"},), provenance=("datago.air_quality",))
    target = ExportTarget(kind="jsonl", output_path="out/data.jsonl")

    _orig_replace = os.replace

    def raise_on_replace(src: str, dst: str) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(os, "replace", raise_on_replace)

    with pytest.raises(ExportError):
        JsonlExporter().export(artifact, target, tmp_path)


def test_markdown_exporter_raises_export_error_on_io_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = ArtifactDataset(records=({"id": "1"},), provenance=("datago.air_quality",))
    target = ExportTarget(kind="markdown", output_path="out/README.md")

    _orig_replace = os.replace

    def raise_on_replace(src: str, dst: str) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(os, "replace", raise_on_replace)

    with pytest.raises(ExportError):
        MarkdownExporter().export(artifact, target, tmp_path)


@pytest.mark.parametrize("evil", ["../escape.jsonl", "../../tmp/evil.jsonl", "/tmp/abs.jsonl"])
def test_exporter_rejects_output_path_traversal(tmp_path: Path, evil: str) -> None:
    # 악의적 output_path가 build 워크스페이스 밖으로 파일을 쓰지 못하게 거부 (#210).
    artifact = ArtifactDataset(records=({"id": "1"},), provenance=("datago.air_quality",))
    target = ExportTarget(kind="jsonl", output_path=evil)

    with pytest.raises(PathTraversalError):
        JsonlExporter().export(artifact, target, tmp_path)


def test_markdown_exporter_returns_export_metadata(tmp_path: Path) -> None:
    # Markdown exporter가 실제 파일 경로와 메타데이터를 반환하는지 검증한다.
    artifact = ArtifactDataset(records=({"id": "1"},), provenance=("datago.air_quality",))
    target = ExportTarget(kind="markdown", output_path="out/README.md")

    result = MarkdownExporter().export(artifact, target, tmp_path)

    assert result.output_path == tmp_path / "out/README.md"
    assert result.file_size > 0
    assert result.format == "markdown"


@pytest.mark.parametrize(
    "exporter_cls,output_path",
    [
        ("JsonlExporter", "out/data.jsonl"),
        ("MarkdownExporter", "out/README.md"),
    ],
)
def test_exporter_leaves_no_temp_file_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exporter_cls: str,
    output_path: str,
) -> None:
    # 쓰기 실패 시 임시 파일(.tmp)이 남지 않아야 한다 (#220).
    from kpubdata_builder.exporters import JsonlExporter, MarkdownExporter  # noqa: F401

    cls = JsonlExporter if exporter_cls == "JsonlExporter" else MarkdownExporter
    artifact = ArtifactDataset(records=({"id": "1"},), provenance=("datago.air_quality",))
    target = ExportTarget(kind="jsonl", output_path=output_path)

    def raise_on_replace(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", raise_on_replace)

    with pytest.raises(ExportError):
        cls().export(artifact, target, tmp_path)

    # 임시 파일이 남아 있으면 안 된다.
    out_dir = tmp_path / "out"
    if out_dir.exists():
        leftovers = [p.name for p in out_dir.iterdir() if p.suffix == ".tmp"]
        assert leftovers == [], f"Temp files leaked: {leftovers}"
