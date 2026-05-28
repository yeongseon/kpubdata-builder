"""내장 내보내기 구현을 위한 내보내기 도구 레지스트리.

이 모듈은 기본 exporter 구현을 import 시점에 등록하여 kind 문자열만으로
구현 객체를 찾을 수 있게 한다.

주요 구성:
    - EXPORTER_REGISTRY: kind -> exporter 인스턴스 매핑
"""

from __future__ import annotations

from .base import BaseExporter, ExportResult, ensure_output_dir
from .csv import CsvExporter
from .jsonl import JsonlExporter
from .markdown import MarkdownExporter
from .parquet import ParquetExporter

EXPORTER_REGISTRY: dict[str, BaseExporter] = {
    "csv": CsvExporter(),
    "jsonl": JsonlExporter(),
    "markdown": MarkdownExporter(),
    "parquet": ParquetExporter(),
}

__all__ = [
    "BaseExporter",
    "CsvExporter",
    "EXPORTER_REGISTRY",
    "ExportResult",
    "JsonlExporter",
    "MarkdownExporter",
    "ParquetExporter",
    "ensure_output_dir",
]
