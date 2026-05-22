"""원격 산출물 게시를 위한 기본 게시 도구 계약."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class BasePublisher(ABC):
    """게시 백엔드를 위한 추상 인터페이스."""

    @property
    @abstractmethod
    def name(self) -> str:
        """게시 도구 식별자를 반환한다."""

    @abstractmethod
    def publish(self, artifact_paths: tuple[Path, ...]) -> None:
        """생성된 산출물 경로를 설정된 대상에 게시한다."""
