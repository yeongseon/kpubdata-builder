"""Exporter registry for built-in exporter implementations."""

from __future__ import annotations

from .base import BaseExporter, ExportResult, ensure_output_dir
from .csv import CsvExporter
from .jsonl import JsonlExporter
from .markdown import MarkdownExporter

EXPORTER_REGISTRY: dict[str, BaseExporter] = {
    "csv": CsvExporter(),
    "jsonl": JsonlExporter(),
    "markdown": MarkdownExporter(),
}

__all__ = [
    "BaseExporter",
    "CsvExporter",
    "EXPORTER_REGISTRY",
    "ExportResult",
    "JsonlExporter",
    "MarkdownExporter",
    "ensure_output_dir",
]
