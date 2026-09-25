"""Kaggle 호환 데이터셋 디렉터리를 생성하는 exporter.

출력 구조::
    {output_dir}/{output_path}       ← CSV 데이터 파일
    {output_dir}/dataset-metadata.json  ← Kaggle 메타데이터
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import tempfile
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
            fd, tmp_name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp_name, destination)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
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
        # 라이선스를 추측하지 않는다. 예전에는 선언이 없으면 조용히 CC-BY-4.0 을
        # 적었는데, 이 파일은 Kaggle 이 그대로 읽는 정본이므로 그건 남의 데이터에
        # 대해 사실이 아닌 주장을 대신 해 주는 것이다. 공공누리 제2~4유형처럼
        # 상업적 이용이나 변형이 제한된 데이터라면 명백한 오표기다.
        declared_license = artifact.metadata.get("license")
        if not isinstance(declared_license, str) or not declared_license.strip():
            raise ExportError(
                "Kaggle export requires an explicit license: set 'license' on the BuildSpec. "
                "It is written to dataset-metadata.json, which Kaggle treats as authoritative."
            )
        metadata["licenses"] = [{"name": declared_license.strip()}]

        try:
            fd, tmp_meta = tempfile.mkstemp(dir=metadata_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
                os.replace(tmp_meta, metadata_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_meta)
                raise
        except OSError as exc:
            raise ExportError(f"Failed to write Kaggle metadata to {metadata_path}: {exc}") from exc

        return ExportResult(
            output_path=destination,
            file_size=destination.stat().st_size,
            format=self.name,
        )
