"""JSONL 내보내기 도구 스텁 구현."""

from __future__ import annotations

import json
from pathlib import Path

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from .base import BaseExporter, ExportResult, ensure_output_dir


class JsonlExporter(BaseExporter):
    """레코드를 줄바꿈 구분 JSON으로 기록하는 내보내기 도구."""

    @property
    def name(self) -> str:
        """내보내기 도구 이름을 반환한다."""
        return "jsonl"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        """표준 레코드를 JSONL 파일로 내보낸다."""
        destination = ensure_output_dir(output_dir, target.output_path)
        content = "\n".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) for record in artifact.records
        )
        try:
            if content:
                _ = destination.write_text(f"{content}\n", encoding="utf-8")
            else:
                _ = destination.write_text("", encoding="utf-8")
        except OSError as exc:
            raise ExportError(f"Failed to export JSONL artifact to {destination}: {exc}") from exc

        return ExportResult(
            output_path=destination, file_size=destination.stat().st_size, format=self.name
        )
