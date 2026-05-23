"""원격 산출물 게시를 위한 기본 게시 도구 계약.

이 모듈은 빌드가 생성한 파일 묶음을 외부 대상에 전송하는 publisher 구현이
따라야 할 최소 인터페이스를 정의한다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class BasePublisher(ABC):
    """게시 백엔드를 위한 추상 인터페이스.

    구현체는 name 식별자와 publish 메서드를 제공해야 한다.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """게시 도구 식별자를 반환한다."""

    @abstractmethod
    def publish(self, artifact_paths: tuple[Path, ...]) -> None:
        """생성된 산출물 경로를 설정된 대상에 게시한다.

        매개변수:
            artifact_paths: 게시 대상 파일 경로 튜플.
        """
