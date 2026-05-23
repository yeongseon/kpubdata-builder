"""로컬 파일시스템 게시 도구 (#28).

생성된 산출물 파일을 로컬 레지스트리 디렉터리로 복사하여 등록한다. 원격
업로드 없이 Exporter/Publisher 경계를 검증할 수 있는 가장 단순한 publisher다.

주요 구성:
    - LocalPublisher: 로컬 디렉터리 등록 publisher
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .base import BasePublisher, PublishResult


class LocalPublisher(BasePublisher):
    """산출물을 로컬 레지스트리 디렉터리로 복사·등록하는 publisher."""

    @property
    def name(self) -> str:
        """게시 도구 식별자."""
        return "local"

    def publish(self, artifact_paths: tuple[Path, ...], *, destination: str) -> PublishResult:
        """산출물 파일을 destination 디렉터리로 복사하고 결과를 반환한다.

        매개변수:
            artifact_paths: 복사할 산출물 파일 경로.
            destination: 대상 로컬 디렉터리 경로.

        반환값:
            PublishResult: 게시 위치와 개수.
        """
        dest_dir = Path(destination)
        dest_dir.mkdir(parents=True, exist_ok=True)
        for path in artifact_paths:
            _ = shutil.copy2(path, dest_dir / path.name)
        return PublishResult(
            publisher=self.name,
            reference=str(dest_dir),
            artifact_count=len(artifact_paths),
        )


__all__ = ["LocalPublisher"]
