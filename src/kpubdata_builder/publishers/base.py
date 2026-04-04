"""Base publisher contract for remote artifact publishing."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class BasePublisher(ABC):
    """Abstract interface for publication backends."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return publisher identifier."""

    @abstractmethod
    def publish(self, artifact_paths: tuple[Path, ...]) -> None:
        """Publish generated artifact paths to the configured destination."""
