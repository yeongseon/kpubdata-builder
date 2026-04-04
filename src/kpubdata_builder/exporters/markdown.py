"""Markdown exporter stub implementation."""

from __future__ import annotations

from pathlib import Path

from ..artifact import ArtifactDataset
from ..spec import ExportTarget
from .base import BaseExporter


class MarkdownExporter(BaseExporter):
    """Exporter that emits a simple Markdown dataset summary."""

    @property
    def name(self) -> str:
        """Return exporter name."""
        return "markdown"

    def export(self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path) -> Path:
        """Export artifact metadata and row count as Markdown."""
        destination = output_dir / target.output_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Dataset Artifact",
            "",
            f"- Records: {len(artifact.records)}",
            f"- Provenance entries: {len(artifact.provenance)}",
        ]
        _ = destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return destination
