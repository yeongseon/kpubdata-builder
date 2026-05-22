"""내장 내보내기 구현을 위한 내보내기 도구 레지스트리."""

from __future__ import annotations

from .base import BaseExporter, ExportResult, ensure_output_dir
from .jsonl import JsonlExporter
from .markdown import MarkdownExporter

EXPORTER_REGISTRY: dict[str, BaseExporter] = {
    "jsonl": JsonlExporter(),
    "markdown": MarkdownExporter(),
}

__all__ = [
    "BaseExporter",
    "EXPORTER_REGISTRY",
    "ExportResult",
    "JsonlExporter",
    "MarkdownExporter",
    "ensure_output_dir",
]
