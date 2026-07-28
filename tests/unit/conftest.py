"""테스트 설정 fixture (#326).

이 모듈은 모든 테스트에서 공통으로 사용하는 fixture를 정의한다.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from kpubdata_builder.exporters import clear_exporter_registry


@pytest.fixture(autouse=True)
def _isolate_exporter_registry() -> Iterator[None]:
    """모든 테스트 전후로 exporter 레지스트리를 격리한다 (#326).

    테스트 간 exporter 등록 누수를 방지하기 위해, 각 테스트 시작 전에
    내장 exporter를 재등록하고, 테스트 종료 후 레지스트리를 비운다.
    """
    from kpubdata_builder.exporters import (
        CsvExporter,
        HuggingFaceExporter,
        JsonlExporter,
        KaggleExporter,
        MarkdownExporter,
        ParquetExporter,
        register_exporter,
    )

    # 테스트 시작 전 내장 exporter 재등록
    clear_exporter_registry()
    register_exporter(CsvExporter(), override=True)
    register_exporter(HuggingFaceExporter(), override=True)
    register_exporter(JsonlExporter(), override=True)
    register_exporter(MarkdownExporter(), override=True)
    register_exporter(KaggleExporter(), override=True)
    register_exporter(ParquetExporter(), override=True)

    yield

    # 테스트 종료 후 레지스트리 비우기
    clear_exporter_registry()
