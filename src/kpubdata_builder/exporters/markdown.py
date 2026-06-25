"""마크다운 내보내기 도구: ArtifactDataset을 사람이 읽기 좋은 문서로 변환한다."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from .base import BaseExporter, ExportResult, ensure_output_dir

SAMPLE_ROW_LIMIT = 5


class MarkdownExporter(BaseExporter):
    """데이터셋을 README 형태의 마크다운 문서로 출력하는 내보내기 도구."""

    @property
    def name(self) -> str:
        """내보내기 도구 이름을 반환한다."""
        return "markdown"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        """아티팩트를 사람이 읽기 좋은 마크다운 문서로 내보낸다."""
        destination = ensure_output_dir(output_dir, target.output_path)
        content = _render_markdown(artifact)
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
            raise ExportError(
                f"Failed to export Markdown artifact to {destination}: {exc}"
            ) from exc
        return ExportResult(
            output_path=destination, file_size=destination.stat().st_size, format=self.name
        )


def _render_markdown(artifact: ArtifactDataset) -> str:
    """아티팩트의 각 섹션을 조립해 마크다운 문서를 만든다."""
    lines: list[str] = []
    lines.extend(_title_section(artifact))
    lines.extend(_schema_section(artifact))
    lines.extend(_sample_section(artifact))
    lines.extend(_provenance_section(artifact))
    return "\n".join(lines) + "\n"


def _title_section(artifact: ArtifactDataset) -> list[str]:
    """메타데이터의 제목과 설명, 레코드 수를 출력한다."""
    title = artifact.metadata.get("title", "Dataset Artifact")
    lines = [f"# {title}", ""]
    description = artifact.metadata.get("description")
    if description:
        lines.extend([description, ""])
    lines.extend([f"- Records: {len(artifact.records)}", ""])
    return lines


def _column_names(artifact: ArtifactDataset) -> list[str]:
    """스키마의 컬럼명을 반환하고, 없으면 모든 레코드의 키를 합산한다."""
    if artifact.schema:
        return list(artifact.schema.keys())
    if artifact.records:
        columns: dict[str, None] = {}
        for record in artifact.records:
            for key in record:
                columns.setdefault(key, None)
        return list(columns.keys())
    return []


def _schema_section(artifact: ArtifactDataset) -> list[str]:
    """스키마를 마크다운 테이블(field | type)로 출력한다."""
    lines = ["## Schema", ""]
    columns = _column_names(artifact)
    if not columns:
        lines.extend(["_No schema available._", ""])
        return lines
    lines.append("| field | type |")
    lines.append("| --- | --- |")
    for name in columns:
        dtype = artifact.schema.get(name, "unknown") if artifact.schema else "unknown"
        lines.append(f"| {name} | {dtype} |")
    lines.append("")
    return lines


def _sample_section(artifact: ArtifactDataset) -> list[str]:
    """최대 SAMPLE_ROW_LIMIT개의 레코드를 마크다운 테이블로 출력한다."""
    lines = ["## Sample Rows", ""]
    if not artifact.records:
        lines.extend(["_No records available._", ""])
        return lines
    columns = _column_names(artifact)
    lines.append("| " + " | ".join(columns) + " |")
    lines.append("| " + " | ".join("---" for _ in columns) + " |")
    for record in artifact.records[:SAMPLE_ROW_LIMIT]:
        cells = [_format_cell(record.get(col)) for col in columns]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def _provenance_section(artifact: ArtifactDataset) -> list[str]:
    """provenance 항목(출처 이름)을 불릿 목록으로 출력한다."""
    lines = ["## Provenance", ""]
    if not artifact.provenance:
        lines.extend(["_No provenance recorded._", ""])
        return lines
    for source in artifact.provenance:
        lines.append(f"- {source}")
    lines.append("")
    return lines


def _format_cell(value: object) -> str:
    """마크다운 테이블 셀 값을 안전하게 문자열로 변환한다."""
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")
