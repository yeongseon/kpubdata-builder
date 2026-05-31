"""JSONL exporter stub implementation."""

from __future__ import annotations

import json
from pathlib import Path

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from .base import BaseExporter, ExportResult, ensure_output_dir


class JsonlExporter(BaseExporter):
    """Exporter that writes records as newline-delimited JSON."""

    @property
    def name(self) -> str:
        """Return exporter name."""
        return "jsonl"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        """Export canonical records to a JSONL file."""
        destination = ensure_output_dir(output_dir, target.output_path)
        try:
            with destination.open("w", encoding="utf-8") as f:
                for record in artifact.records:
                    f.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
                    f.write("\n")
        except OSError as exc:
            raise ExportError(f"Failed to export JSONL artifact to {destination}: {exc}") from exc

        return ExportResult(
            output_path=destination, file_size=destination.stat().st_size, format=self.name
        )
