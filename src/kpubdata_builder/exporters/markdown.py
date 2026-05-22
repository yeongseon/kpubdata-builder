"""마크다운 내보내기 도구 스텁 구현."""

from __future__ import annotations

from pathlib import Path

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from .base import BaseExporter, ExportResult, ensure_output_dir


class MarkdownExporter(BaseExporter):
    """간단한 마크다운 데이터셋 요약을 출력하는 내보내기 도구."""

    @property
    def name(self) -> str:
        """내보내기 도구 이름을 반환한다."""
        return "markdown"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        """산출물 메타데이터와 행 수를 마크다운으로 내보낸다."""
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
