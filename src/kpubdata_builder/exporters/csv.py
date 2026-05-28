"""CSV exporter implementation."""

from __future__ import annotations

import csv
import io
from pathlib import Path

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from .base import BaseExporter, ExportResult, ensure_output_dir


class CsvExporter(BaseExporter):
    """Exporter that writes records as comma-separated values."""

    @property
    def name(self) -> str:
        return "csv"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        destination = ensure_output_dir(output_dir, target.output_path)

        if not artifact.records:
            content = ""
        else:
            fieldnames = list(artifact.records[0].keys())
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for record in artifact.records:
                writer.writerow(record)
            content = buf.getvalue()

        try:
            _ = destination.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ExportError(f"Failed to export CSV artifact to {destination}: {exc}") from exc

        return ExportResult(
            output_path=destination, file_size=destination.stat().st_size, format=self.name
        )
