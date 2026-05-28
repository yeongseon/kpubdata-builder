"""Tests for build manifest model."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from kpubdata_builder import ManifestError
from kpubdata_builder.manifest import BuildManifest, SourceProvenance, manifest_writer


def test_build_manifest_instantiation() -> None:
    """BuildManifest can be instantiated with required fields."""
    started_at = datetime.now(tz=timezone.utc)
    finished_at = datetime.now(tz=timezone.utc)

    manifest = BuildManifest(build_id="build-1", started_at=started_at, finished_at=finished_at)

    assert manifest.build_id == "build-1"


def test_manifest_writer_creates_parent_directories(tmp_path: Path) -> None:
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime.now(tz=timezone.utc),
        finished_at=datetime.now(tz=timezone.utc),
    )
    output_path = tmp_path / "nested" / "build" / "manifest.json"

    manifest_writer(manifest, output_path)

    assert output_path.exists()


def test_manifest_writer_wraps_io_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime.now(tz=timezone.utc),
        finished_at=datetime.now(tz=timezone.utc),
    )
    output_path = tmp_path / "manifest.json"

    def raise_io_error(self: Path, data: str, *, encoding: str) -> int:
        del self, data, encoding
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", raise_io_error)

    with pytest.raises(ManifestError):
        manifest_writer(manifest, output_path)


# --- provenance in manifest ---


def test_manifest_writer_includes_provenance(tmp_path: Path) -> None:
    prov = SourceProvenance(
        provider="datago",
        dataset="apt_trade",
        fetched_at="2026-05-08T12:00:00+00:00",
        params={"lawd_cd": "11680"},
        record_count=10,
        data_checksum="abc123",
    )
    manifest = BuildManifest(
        build_id="build-prov",
        started_at=datetime.now(tz=timezone.utc),
        finished_at=datetime.now(tz=timezone.utc),
        provenance=(prov,),
    )
    output_path = tmp_path / "manifest.json"
    manifest_writer(manifest, output_path)

    import json

    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(data["provenance"]) == 1
    p = data["provenance"][0]
    assert p["provider"] == "datago"
    assert p["dataset"] == "apt_trade"
    assert p["record_count"] == 10
    assert p["data_checksum"] == "abc123"
    assert p["params"] == {"lawd_cd": "11680"}


def test_manifest_provenance_defaults_to_empty(tmp_path: Path) -> None:
    manifest = BuildManifest(
        build_id="build-no-prov",
        started_at=datetime.now(tz=timezone.utc),
        finished_at=datetime.now(tz=timezone.utc),
    )
    output_path = tmp_path / "manifest.json"
    manifest_writer(manifest, output_path)

    import json

    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert data["provenance"] == []
