"""Publisher 경계(#28): PublishResult 계약과 LocalPublisher 등록 동작 검증."""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.publishers import (
    PUBLISHER_REGISTRY,
    BasePublisher,
    LocalPublisher,
    PublishResult,
)


def _make_artifacts(tmp_path: Path) -> tuple[Path, ...]:
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.md"
    _ = a.write_text("{}\n", encoding="utf-8")
    _ = b.write_text("# title\n", encoding="utf-8")
    return (a, b)


class TestLocalPublisher:
    def test_is_a_base_publisher_named_local(self) -> None:
        publisher = LocalPublisher()
        assert isinstance(publisher, BasePublisher)
        assert publisher.name == "local"

    def test_copies_artifacts_into_destination_and_reports(self, tmp_path: Path) -> None:
        artifacts = _make_artifacts(tmp_path)
        destination = tmp_path / "registry" / "dataset-v1"

        result = LocalPublisher().publish(artifacts, destination=str(destination))

        assert isinstance(result, PublishResult)
        assert result.publisher == "local"
        assert result.status == "ok"
        assert result.artifact_count == 2
        assert (destination / "a.jsonl").exists()
        assert (destination / "b.md").exists()
        assert result.reference == str(destination)

    def test_registered_in_publisher_registry(self) -> None:
        assert "local" in PUBLISHER_REGISTRY
        assert isinstance(PUBLISHER_REGISTRY["local"], LocalPublisher)


def test_publish_result_is_immutable() -> None:
    result = PublishResult(publisher="local", reference="/tmp/x", artifact_count=1)
    with pytest.raises(AttributeError):
        result.publisher = "other"  # type: ignore[misc]
