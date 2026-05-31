"""Kaggle 호환 데이터셋 디렉터리를 생성하는 exporter.

출력 구조::
    {output_dir}/{output_path}       ← CSV 데이터 파일
    {output_dir}/dataset-metadata.json  ← Kaggle 메타데이터
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from .base import BaseExporter, ExportResult, ensure_output_dir


class KaggleExporter(BaseExporter):
    """Kaggle 형식(CSV + dataset-metadata.json)으로 내보내는 exporter."""

    @property
    def name(self) -> str:
        return "kaggle"

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
            raise ExportError(f"Failed to export Kaggle artifact to {destination}: {exc}") from exc

        metadata_path = destination.parent / "dataset-metadata.json"
        dataset_id = artifact.metadata.get("dataset_id", "unknown/dataset")
        metadata = {
            "title": artifact.metadata.get("title", "Dataset"),
            "id": dataset_id,
            "licenses": [{"name": "CC-BY-4.0"}],
            "resources": [{"path": destination.name, "description": "Main dataset file"}],
        }
        try:
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise ExportError(f"Failed to write Kaggle metadata to {metadata_path}: {exc}") from exc

        return ExportResult(
            output_path=destination,
            file_size=destination.stat().st_size,
            format=self.name,
        )
