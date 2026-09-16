"""영속 Build 저장소 (#309, ADR 0003; 백엔드 분리 ADR 0010/0016).

빌드 인덱스를 제공하여 파일시스템 스캔 비용을 줄이고 확장성/일관성/동시성을 개선한다.
``BuildIndex`` 는 Protocol 이고 기본 구현체는 ``SqliteBuildIndex``(무외부의존)다.
``make_build_index()`` 팩토리가 ``KPUBDATA_BUILDER_STORAGE_BACKEND`` 에 따라 sqlite/cubrid
구현체를 선택한다. ``CubridBuildIndex`` 는 cubrid 선택 시에만 lazy import 된다.
"""

from __future__ import annotations

from .build_index import (
    SCHEMA_VERSION,
    BuildEntry,
    BuildIndex,
    SqliteBuildIndex,
    make_build_index,
    rebuild_index,
)

__all__ = [
    "BuildEntry",
    "BuildIndex",
    "SCHEMA_VERSION",
    "SqliteBuildIndex",
    "make_build_index",
    "rebuild_index",
]
