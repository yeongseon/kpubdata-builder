"""JSONL exporter stub implementation."""

from __future__ import annotations

import json
from pathlib import Path

from ..artifact import ArtifactDataset
from ..spec import ExportTarget
from .base import BaseExporter


class JsonlExporter(BaseExporter):
    """Exporter that writes records as newline-delimited JSON."""

    @property
    def name(self) -> str:
        """Return exporter name."""
        return "jsonl"

    def export(self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path) -> Path:
        """Export canonical records to a JSONL file."""
        destination = output_dir / target.output_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) for record in artifact.records
        )
        if content:
            _ = destination.write_text(f"{content}\n", encoding="utf-8")
        else:
            _ = destination.write_text("", encoding="utf-8")
        return destination
