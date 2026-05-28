"""Exporter registry for built-in exporter implementations."""

from __future__ import annotations

from .base import BaseExporter, ExportResult, ensure_output_dir
from .jsonl import JsonlExporter
from .kaggle import KaggleExporter
from .markdown import MarkdownExporter

EXPORTER_REGISTRY: dict[str, BaseExporter] = {
    "jsonl": JsonlExporter(),
    "kaggle": KaggleExporter(),
    "markdown": MarkdownExporter(),
}

__all__ = [
    "BaseExporter",
    "EXPORTER_REGISTRY",
    "ExportResult",
    "JsonlExporter",
    "KaggleExporter",
    "MarkdownExporter",
    "ensure_output_dir",
]
