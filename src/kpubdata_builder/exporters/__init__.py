"""내장 내보내기 구현과 플러그인 레지스트리.

내장 exporter를 import 시점에 레지스트리에 등록하고, 제3자 exporter 등록·발견을
위한 플러그인 API(registry.py)를 함께 노출한다.

주요 구성:
    - _EXPORTER_FACTORIES: kind -> factory 매핑 (ADR 0004)
    - EXPORTER_REGISTRY: kind -> exporter 인스턴스 매핑 (레거시 호환)
    - register_exporter_factory / register_exporter_instance: 등록 API
    - get_exporter / load_entry_point_exporters: 조회/발견 API
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
    clear_exporter_registry,
    get_exporter,
    load_entry_point_exporters,
    register_exporter,
    register_exporter_factory,
    register_exporter_instance,
)

# 내장 exporter 등록 (ADR 0004 권고: factory 방식).
# override=True는 재import 시 덮어쓰기 위함 (개발 중 편의).
# 인스턴스 레지스트리에도 등록하여 하위 호환성 보장 (#325).
register_exporter_factory("csv", CsvExporter, override=True)
register_exporter_factory("huggingface", HuggingFaceExporter, override=True)
register_exporter_factory("jsonl", JsonlExporter, override=True)
register_exporter_factory("markdown", MarkdownExporter, override=True)
register_exporter_factory("kaggle", KaggleExporter, override=True)
register_exporter_factory("parquet", ParquetExporter, override=True)

# 레거시 코드가 EXPORTER_REGISTRY를 직접 조회하는 경우를 위해 인스턴스도 등록
register_exporter_instance(CsvExporter(), override=True)
register_exporter_instance(HuggingFaceExporter(), override=True)
register_exporter_instance(JsonlExporter(), override=True)
register_exporter_instance(MarkdownExporter(), override=True)
register_exporter_instance(KaggleExporter(), override=True)
register_exporter_instance(ParquetExporter(), override=True)

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
    "clear_exporter_registry",
    "ensure_output_dir",
    "get_exporter",
    "load_entry_point_exporters",
    "register_exporter",
    "register_exporter_factory",
    "register_exporter_instance",
]
