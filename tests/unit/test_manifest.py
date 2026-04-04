"""Tests for build manifest model."""

from __future__ import annotations

from datetime import datetime, timezone

from kpubdata_builder.manifest import BuildManifest


def test_build_manifest_instantiation() -> None:
    """BuildManifest can be instantiated with required fields."""
    started_at = datetime.now(tz=timezone.utc)
    finished_at = datetime.now(tz=timezone.utc)

    manifest = BuildManifest(build_id="build-1", started_at=started_at, finished_at=finished_at)

    assert manifest.build_id == "build-1"
