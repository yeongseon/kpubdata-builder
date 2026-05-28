"""Publisher 경계(#28): PublishResult 계약과 LocalPublisher 등록 동작 검증."""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.errors import PublishError
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


class TestLocalPublisherFailurePolicy:
    def test_rejects_duplicate_basenames(self, tmp_path: Path) -> None:
        # 서로 다른 디렉터리의 동일 이름 파일 두 개는 flat copy에서 한쪽을 덮어쓰므로 거부.
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        f1 = d1 / "data.jsonl"
        f2 = d2 / "data.jsonl"
        _ = f1.write_text("{}\n", encoding="utf-8")
        _ = f2.write_text("{}\n", encoding="utf-8")

        with pytest.raises(PublishError, match="duplicate artifact basename"):
            LocalPublisher().publish((f1, f2), destination=str(tmp_path / "registry"))

    def test_rejects_directory_artifacts(self, tmp_path: Path) -> None:
        # 디렉터리는 shutil.copy2가 다룰 수 없으므로 명시적으로 거부.
        dir_artifact = tmp_path / "hf_layout"
        dir_artifact.mkdir()

        with pytest.raises(PublishError, match="directory artifacts"):
            LocalPublisher().publish((dir_artifact,), destination=str(tmp_path / "registry"))

    def test_wraps_copy_failures_in_publish_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # shutil.copy2가 OSError를 던지면 PublishError로 감싸 전파해야 한다.
        artifact = tmp_path / "data.jsonl"
        _ = artifact.write_text("{}\n", encoding="utf-8")

        def _boom(src: object, dst: object) -> None:
            raise PermissionError("read-only filesystem")

        monkeypatch.setattr("kpubdata_builder.publishers.local.shutil.copy2", _boom)

        with pytest.raises(PublishError, match="failed to copy"):
            LocalPublisher().publish((artifact,), destination=str(tmp_path / "registry"))
