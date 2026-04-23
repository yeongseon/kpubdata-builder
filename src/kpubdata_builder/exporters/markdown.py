"""Markdown exporter stub implementation."""

from __future__ import annotations

from pathlib import Path

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from .base import BaseExporter, ExportResult, ensure_output_dir


class MarkdownExporter(BaseExporter):
    """Exporter that emits a simple Markdown dataset summary."""

    @property
    def name(self) -> str:
        """Return exporter name."""
        return "markdown"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        """Export artifact metadata and row count as Markdown."""
        destination = ensure_output_dir(output_dir, target.output_path)
        lines = [
            "# Dataset Artifact",
            "",
            f"- Records: {len(artifact.records)}",
            f"- Provenance entries: {len(artifact.provenance)}",
        ]
        try:
            _ = destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            raise ExportError(
                f"Failed to export Markdown artifact to {destination}: {exc}"
            ) from exc

        return ExportResult(
            output_path=destination, file_size=destination.stat().st_size, format=self.name
        )
