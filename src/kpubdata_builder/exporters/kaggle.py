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
from typing import Any

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from .base import BaseExporter, ExportResult, ensure_output_dir
from .csv import _format_cell, _resolve_columns


class KaggleExporter(BaseExporter):
    """Kaggle 형식(CSV + dataset-metadata.json)으로 내보내는 exporter."""

    @property
    def name(self) -> str:
        return "kaggle"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        destination = ensure_output_dir(output_dir, target.output_path)
        columns = _resolve_columns(artifact)

        buffer = io.StringIO()
        if columns:
            writer = csv.writer(buffer, lineterminator="\n")
            writer.writerow(columns)
            for record in artifact.records:
                writer.writerow([_format_cell(record.get(column)) for column in columns])
        content = buffer.getvalue()

        try:
            _ = destination.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ExportError(f"Failed to export Kaggle artifact to {destination}: {exc}") from exc

        metadata_path = destination.parent / "dataset-metadata.json"
        resource = {"path": destination.name, "description": "Main dataset file"}
        metadata: dict[str, Any]

        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ExportError(
                    f"Failed to read existing Kaggle metadata at {metadata_path}: {exc}"
                ) from exc
            if not isinstance(metadata, dict):
                metadata = {}
            resources_obj = metadata.get("resources")
            resources = resources_obj if isinstance(resources_obj, list) else []
            if not any(
                isinstance(entry, dict) and entry.get("path") == resource["path"]
                for entry in resources
            ):
                resources.append(resource)
            metadata["resources"] = resources
        else:
            metadata = {"resources": [resource]}

        # id/title/licenses는 권한적(authoritative) 필드이므로 매 export 시 현재 artifact
        # 값으로 갱신한다. 기존 파일에서 stale 값이 그대로 남으면 publisher 검증 실패나
        # 잘못된 Kaggle 데이터셋 업로드로 이어질 수 있다 (#202). 그 외 키는 보존한다.
        metadata["title"] = artifact.metadata.get("title", "Dataset")
        metadata["id"] = artifact.metadata.get("dataset_id", "unknown/dataset")
        metadata["licenses"] = [{"name": artifact.metadata.get("license", "CC-BY-4.0")}]

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
