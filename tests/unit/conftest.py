"""테스트 설정 fixture (#326).

이 모듈은 모든 테스트에서 공통으로 사용하는 fixture를 정의한다.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from kpubdata_builder.exporters import EXPORTER_REGISTRY


@pytest.fixture(autouse=True)
def _isolate_exporter_registry() -> Iterator[None]:
    """모든 테스트 전후로 exporter 레지스트를 격리한다 (#326).

    테스트 간 exporter 등록 누수를 방지하기 위해, factory와 인스턴스
    레지스트를 모두 스냅샷한 뒤 테스트 종료 시 원래 상태로 복원한다.
    이렇게 하면 내장 exporter factory 등록(#325)이 테스트 간에 유지된다.
    """
    import kpubdata_builder.exporters.registry as reg_module

    factory_snapshot = dict(reg_module._EXPORTER_FACTORIES)
    instance_snapshot = dict(EXPORTER_REGISTRY)
    try:
        yield
    finally:
        reg_module._EXPORTER_FACTORIES.clear()
        reg_module._EXPORTER_FACTORIES.update(factory_snapshot)
        EXPORTER_REGISTRY.clear()
        EXPORTER_REGISTRY.update(instance_snapshot)
