"""로컬 파일시스템 게시 도구 (#28).

생성된 산출물 파일을 로컬 레지스트리 디렉터리로 복사하여 등록한다. 원격
업로드 없이 Exporter/Publisher 경계를 검증할 수 있는 가장 단순한 publisher다.

주요 구성:
    - LocalPublisher: 로컬 디렉터리 등록 publisher
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..errors import PublishError
from .base import BasePublisher, PublishResult


class LocalPublisher(BasePublisher):
    """산출물을 로컬 레지스트리 디렉터리로 복사·등록하는 publisher.

    실패 정책:
        - 디렉터리 artifact는 명시적으로 거부한다(`PublishError`). 디렉터리 레이아웃
          게시는 별도 publisher의 책임이며, 여기서 무음 복사 형태로 지원하지 않는다.
        - 서로 다른 경로지만 basename이 충돌하는 artifact는 거부한다 — 데이터 손실을
          막기 위한 명시적 실패를 선택한다.
        - copy 자체가 실패하면 `OSError`를 `PublishError`로 감싸 전파한다.
    """

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

        예외:
            PublishError: 디렉터리 artifact / basename 충돌 / 복사 I/O 실패.
        """
        # 디렉터리 거부: shutil.copy2는 디렉터리를 다루지 못하므로 사전에 명확한 에러를 던진다.
        directories = [p for p in artifact_paths if p.is_dir()]
        if directories:
            offenders = ", ".join(str(p) for p in directories)
            raise PublishError(
                f"directory artifacts are not supported by LocalPublisher: {offenders}"
            )

        # basename 충돌 거부: flat copy 정책에서 같은 이름이면 한쪽이 묻히므로 명시적으로 실패.
        names: dict[str, Path] = {}
        for path in artifact_paths:
            existing = names.get(path.name)
            if existing is not None and existing != path:
                raise PublishError(
                    f"duplicate artifact basename {path.name!r}: "
                    f"{existing} and {path} cannot share the destination directory"
                )
            names[path.name] = path

        dest_dir = Path(destination)
        dest_dir.mkdir(parents=True, exist_ok=True)
        for path in artifact_paths:
            try:
                _ = shutil.copy2(path, dest_dir / path.name)
            except OSError as exc:
                raise PublishError(f"failed to copy {path} → {dest_dir}: {exc}") from exc
        return PublishResult(
            publisher=self.name,
            reference=str(dest_dir),
            artifact_count=len(artifact_paths),
        )


__all__ = ["LocalPublisher"]
