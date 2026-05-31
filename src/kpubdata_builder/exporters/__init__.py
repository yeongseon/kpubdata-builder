"""내장 내보내기 구현과 플러그인 레지스트리.

내장 exporter를 import 시점에 레지스트리에 등록하고, 제3자 exporter 등록·발견을
위한 플러그인 API(registry.py)를 함께 노출한다.

주요 구성:
    - EXPORTER_REGISTRY: kind -> exporter 인스턴스 매핑
    - register_exporter / get_exporter / load_entry_point_exporters: 플러그인 API
"""

from __future__ import annotations

from .base import BaseExporter, ExportResult, ensure_output_dir
from .csv import CsvExporter
from .huggingface import HuggingFaceExporter
from .jsonl import JsonlExporter
from .kaggle import KaggleExporter
from .markdown import MarkdownExporter
from .parquet import ParquetExporter
from .registry import (
    EXPORTER_ENTRY_POINT_GROUP,
    EXPORTER_REGISTRY,
    get_exporter,
    load_entry_point_exporters,
    register_exporter,
)

# 내장 exporter 등록 (이미 등록된 경우 재import 시 덮어쓴다).
register_exporter(CsvExporter(), override=True)
register_exporter(HuggingFaceExporter(), override=True)
register_exporter(JsonlExporter(), override=True)
register_exporter(MarkdownExporter(), override=True)
register_exporter(KaggleExporter(), override=True)
register_exporter(ParquetExporter(), override=True)

__all__ = [
    "EXPORTER_ENTRY_POINT_GROUP",
    "EXPORTER_REGISTRY",
    "BaseExporter",
    "CsvExporter",
    "ExportResult",
    "HuggingFaceExporter",
    "JsonlExporter",
    "KaggleExporter",
    "MarkdownExporter",
    "ParquetExporter",
    "ensure_output_dir",
    "get_exporter",
    "load_entry_point_exporters",
    "register_exporter",
]
