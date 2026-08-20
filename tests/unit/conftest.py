"""테스트 설정 fixture (#326).

이 모듈은 모든 테스트에서 공통으로 사용하는 fixture를 정의한다.
Windows 크로스플랫폼 판정 헬퍼(#553)도 여기에 둔다.
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from kpubdata_builder.exporters import EXPORTER_REGISTRY


def symlinks_supported() -> bool:
    """현재 권한/파일시스템에서 symlink 생성이 가능한지 (#553).

    Windows는 관리자 권한 또는 Developer Mode 없이는 symlink_to가
    OSError를 낸다 — symlink 동작 자체가 주제인 테스트는 이 값으로 skip한다.
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            link = Path(tmp) / "link"
            link.symlink_to(target, target_is_directory=True)
            return True
    except OSError:
        return False


def spawn_timeout_multiplier() -> float:
    """process spawn 기반 테스트의 timeout 배율 (#553).

    Windows spawn은 자식 인터프리터가 모듈(polars 포함)을 새로 import하므로
    Linux 대비 수 초 이상 느릴 수 있다 — 시간 자체가 주제가 아닌 테스트의
    timeout을 플랫폼 배율로 넉넉하게 잡는다.
    """
    return 3.0 if sys.platform == "win32" else 1.0


requires_symlinks = pytest.mark.skipif(
    not symlinks_supported(), reason="symlink creation is not permitted on this platform (#553)"
)


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
