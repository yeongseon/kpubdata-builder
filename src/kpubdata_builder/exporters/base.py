"""Base exporter contract for artifact output generation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..artifact import ArtifactDataset
from ..spec import ExportTarget


class BaseExporter(ABC):
    """Abstract base class for all artifact exporters."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return exporter identifier used by registry and specs."""

    @abstractmethod
    def export(self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path) -> Path:
        """Write output artifact and return the generated file path."""
