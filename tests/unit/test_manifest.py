"""Tests for build manifest model."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from kpubdata_builder import ManifestError
from kpubdata_builder.manifest import (
    BuildManifest,
    FieldSummary,
    SchemaSummary,
    extract_schema_summary,
    manifest_writer,
)


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


# --- extract_schema_summary ---


def test_extract_schema_summary_empty_records() -> None:
    assert extract_schema_summary(()) is None


def test_extract_schema_summary_basic() -> None:
    records: tuple[dict[str, object], ...] = (
        {"name": "Alice", "age": 30},
        {"name": "Bob", "age": 25},
    )
    summary = extract_schema_summary(records)
    assert summary is not None
    assert summary.total_fields == 2
    assert len(summary.fields) == 2

    by_name = {f.name: f for f in summary.fields}
    assert by_name["name"].type == "str"
    assert by_name["name"].nullable is False
    assert by_name["age"].type == "int"
    assert by_name["age"].nullable is False


def test_extract_schema_summary_nullable() -> None:
    records: tuple[dict[str, object], ...] = (
        {"value": 1},
        {"value": None},
    )
    summary = extract_schema_summary(records)
    assert summary is not None
    assert summary.fields[0].nullable is True


def test_extract_schema_summary_missing_key_is_nullable() -> None:
    records: tuple[dict[str, object], ...] = (
        {"a": 1, "b": 2},
        {"a": 3},
    )
    summary = extract_schema_summary(records)
    assert summary is not None
    by_name = {f.name: f for f in summary.fields}
    assert by_name["b"].nullable is True


def test_extract_schema_summary_mixed_types() -> None:
    records: tuple[dict[str, object], ...] = (
        {"val": 1},
        {"val": "hello"},
    )
    summary = extract_schema_summary(records)
    assert summary is not None
    assert summary.fields[0].type == "int, str"


# --- manifest with schema_summary ---


def test_manifest_writer_includes_schema_summary(tmp_path: Path) -> None:
    summary = SchemaSummary(
        fields=(
            FieldSummary(name="id", type="int", nullable=False),
            FieldSummary(name="name", type="str", nullable=True, description="person name"),
        ),
        total_fields=2,
    )
    manifest = BuildManifest(
        build_id="build-schema",
        started_at=datetime.now(tz=timezone.utc),
        finished_at=datetime.now(tz=timezone.utc),
        schema_summary=summary,
    )
    output_path = tmp_path / "manifest.json"
    manifest_writer(manifest, output_path)

    import json as _json

    data = _json.loads(output_path.read_text(encoding="utf-8"))
    assert "schema_summary" in data
    assert data["schema_summary"]["total_fields"] == 2
    assert len(data["schema_summary"]["fields"]) == 2
    assert data["schema_summary"]["fields"][0]["name"] == "id"
    assert data["schema_summary"]["fields"][1]["nullable"] is True


def test_manifest_writer_schema_summary_none(tmp_path: Path) -> None:
    manifest = BuildManifest(
        build_id="build-no-schema",
        started_at=datetime.now(tz=timezone.utc),
        finished_at=datetime.now(tz=timezone.utc),
    )
    output_path = tmp_path / "manifest.json"
    manifest_writer(manifest, output_path)

    import json as _json

    data = _json.loads(output_path.read_text(encoding="utf-8"))
    assert data["schema_summary"] is None
