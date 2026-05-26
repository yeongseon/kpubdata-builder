"""Markdown exporter: render an ArtifactDataset as a human-readable document."""

from __future__ import annotations

from pathlib import Path

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from .base import BaseExporter, ExportResult, ensure_output_dir

SAMPLE_ROW_LIMIT = 5


class MarkdownExporter(BaseExporter):
    """Exporter that emits a README-style Markdown summary of a dataset."""

    @property
    def name(self) -> str:
        """Return exporter name."""
        return "markdown"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        """Export the artifact as a human-readable Markdown document."""
        destination = ensure_output_dir(output_dir, target.output_path)
        content = _render_markdown(artifact)
        try:
            _ = destination.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ExportError(
                f"Failed to export Markdown artifact to {destination}: {exc}"
            ) from exc
        return ExportResult(
            output_path=destination, file_size=destination.stat().st_size, format=self.name
        )


def _render_markdown(artifact: ArtifactDataset) -> str:
    """Assemble the Markdown document from artifact sections."""
    lines: list[str] = []
    lines.extend(_title_section(artifact))
    lines.extend(_schema_section(artifact))
    lines.extend(_sample_section(artifact))
    lines.extend(_provenance_section(artifact))
    return "\n".join(lines) + "\n"


def _title_section(artifact: ArtifactDataset) -> list[str]:
    """Title from metadata, followed by an optional description paragraph."""
    title = artifact.metadata.get("title", "Dataset Artifact")
    lines = [f"# {title}", ""]
    description = artifact.metadata.get("description")
    if description:
        lines.extend([description, ""])
    lines.extend([f"- Records: {len(artifact.records)}", ""])
    return lines


def _column_names(artifact: ArtifactDataset) -> list[str]:
    """Schema column names, falling back to the first record's keys."""
    if artifact.schema:
        return list(artifact.schema.keys())
    if artifact.records:
        return list(artifact.records[0].keys())
    return []


def _schema_section(artifact: ArtifactDataset) -> list[str]:
    """Render the schema as a Markdown table (field | type)."""
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
    """Render up to SAMPLE_ROW_LIMIT records as a Markdown table."""
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
    """List provenance entries (source names) as a bullet list."""
    lines = ["## Provenance", ""]
    if not artifact.provenance:
        lines.extend(["_No provenance recorded._", ""])
        return lines
    for source in artifact.provenance:
        lines.append(f"- {source}")
    lines.append("")
    return lines


def _format_cell(value: object) -> str:
    """Render a cell value safely for a Markdown table."""
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")
