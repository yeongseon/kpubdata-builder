"""빌드 매니페스트 모델과 파일 기록 동작을 검증한다."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from kpubdata_builder import ManifestError
from kpubdata_builder.manifest import BuildManifest, manifest_writer


def test_build_manifest_instantiation() -> None:
    """필수 필드만으로 BuildManifest를 생성할 수 있어야 한다."""
    # 데이터 클래스가 최소 생성 요건을 충족하는지 확인한다.
    started_at = datetime.now(tz=timezone.utc)
    finished_at = datetime.now(tz=timezone.utc)

    manifest = BuildManifest(build_id="build-1", started_at=started_at, finished_at=finished_at)

    assert manifest.build_id == "build-1"


def test_manifest_writer_creates_parent_directories(tmp_path: Path) -> None:
    # 중첩 출력 경로가 존재하지 않아도 부모 디렉터리를 만들어 기록하는지 검증한다.
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime.now(tz=timezone.utc),
        finished_at=datetime.now(tz=timezone.utc),
    )
    output_path = tmp_path / "nested" / "build" / "manifest.json"

    manifest_writer(manifest, output_path)

    assert output_path.exists()


def test_manifest_writer_wraps_io_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 디스크 쓰기 실패가 ManifestError로 래핑되는지 확인한다.
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
