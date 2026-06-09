"""Publisher 경계(#28): PublishResult 계약과 LocalPublisher 등록 동작 검증."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from kpubdata_builder.errors import PublishError
from kpubdata_builder.publishers import (
    PUBLISHER_REGISTRY,
    BasePublisher,
    LocalPublisher,
    PublishResult,
)
from kpubdata_builder.publishers.huggingface import HuggingFacePublisher


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


def _install_fake_hf(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, str]]]:
    """huggingface_hub.HfApi를 가짜로 주입하고 업로드 호출을 기록한다."""
    calls: dict[str, list[dict[str, str]]] = {"files": [], "folders": []}

    class FakeHfApi:
        def __init__(self, token: str | None = None) -> None:
            self._token = token

        def upload_file(
            self,
            *,
            path_or_fileobj: str,
            path_in_repo: str,
            repo_id: str,
            repo_type: str,
            commit_message: str,
        ) -> None:
            calls["files"].append({"path_in_repo": path_in_repo, "src": path_or_fileobj})

        def upload_folder(
            self, *, folder_path: str, repo_id: str, repo_type: str, commit_message: str
        ) -> None:
            calls["folders"].append({"folder_path": folder_path})

    fake_module = types.ModuleType("huggingface_hub")
    fake_module.HfApi = FakeHfApi  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_module)
    monkeypatch.setenv("HF_TOKEN", "test-token")
    return calls


class TestHuggingFacePublisher:
    def test_requires_hf_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_hf(monkeypatch)
        monkeypatch.delenv("HF_TOKEN", raising=False)
        artifact = tmp_path / "data.parquet"
        _ = artifact.write_text("x", encoding="utf-8")

        with pytest.raises(RuntimeError, match="HF_TOKEN"):
            HuggingFacePublisher().publish((artifact,), destination="org/ds")

    def test_preserves_directory_layout_in_repo_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 중첩 디렉터리 shard가 bare filename으로 평탄화되지 않고 상대 경로를 유지해야 한다 (#170).
        calls = _install_fake_hf(monkeypatch)
        (tmp_path / "data").mkdir()
        readme = tmp_path / "README.md"
        shard = tmp_path / "data" / "train.parquet"
        _ = readme.write_text("# t\n", encoding="utf-8")
        _ = shard.write_text("x", encoding="utf-8")

        result = HuggingFacePublisher().publish((readme, shard), destination="org/ds")

        repo_paths = {c["path_in_repo"] for c in calls["files"]}
        assert repo_paths == {"README.md", "data/train.parquet"}
        assert result.artifact_count == 2

    def test_same_basename_different_dirs_do_not_collide(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 동명 파일이라도 디렉터리 구조를 보존하면 서로 다른 repo 경로로 분리되어
        # 무경고 덮어쓰기가 일어나지 않는다 (#170).
        calls = _install_fake_hf(monkeypatch)
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        f1 = d1 / "data.parquet"
        f2 = d2 / "data.parquet"
        _ = f1.write_text("x", encoding="utf-8")
        _ = f2.write_text("y", encoding="utf-8")

        result = HuggingFacePublisher().publish((f1, f2), destination="org/ds")

        repo_paths = {c["path_in_repo"] for c in calls["files"]}
        assert repo_paths == {"a/data.parquet", "b/data.parquet"}
        assert result.artifact_count == 2

    def test_directory_artifacts_use_upload_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _install_fake_hf(monkeypatch)
        layout = tmp_path / "dataset"
        layout.mkdir()
        _ = (layout / "x.parquet").write_text("x", encoding="utf-8")

        result = HuggingFacePublisher().publish((layout,), destination="org/ds")

        assert len(calls["folders"]) == 1
        assert calls["files"] == []
        assert result.artifact_count == 1
