"""산출물/manifest 저장소 추상화 (ADR 0010/0016).

``ArtifactStore`` Protocol 과 기본 구현체 ``LocalArtifactStore``(무외부의존)를 노출하고,
``make_artifact_store()`` 팩토리가 ``KPUBDATA_BUILDER_STORAGE_BACKEND`` 에 따라 sqlite/local
또는 cubrid 구현체를 선택한다. ``CubridArtifactStore`` 는 cubrid 선택 시에만 lazy import 된다.
"""

from __future__ import annotations

from pathlib import Path

from .local import LocalArtifactStore
from .protocol import ArtifactStore


def make_artifact_store(output_root: Path) -> ArtifactStore:
    """선택된 백엔드에 맞는 ``ArtifactStore`` 를 생성한다 (ADR 0016)."""
    from ..backend import storage_backend

    if storage_backend() == "cubrid":
        from ..backend import get_engine
        from .cubrid import CubridArtifactStore

        return CubridArtifactStore(output_root, get_engine())
    return LocalArtifactStore(output_root)


__all__ = ["ArtifactStore", "LocalArtifactStore", "make_artifact_store"]
