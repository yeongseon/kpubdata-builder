"""Base exporter contract for artifact output generation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget


@dataclass(frozen=True)
class ExportResult:
    output_path: Path
    file_size: int
    format: str


def ensure_output_dir(output_dir: Path, relative_output_path: str) -> Path:
    destination = output_dir / relative_output_path
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportError(f"Failed to prepare output directory for {destination}: {exc}") from exc
    return destination


class BaseExporter(ABC):
    """Abstract base class for all artifact exporters."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return exporter identifier used by registry and specs."""

    @abstractmethod
    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        pass


__all__ = ["BaseExporter", "ExportResult", "ensure_output_dir"]
