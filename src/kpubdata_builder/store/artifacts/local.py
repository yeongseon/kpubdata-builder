"""LocalArtifactStore — 로컬 파일시스템 기반 산출물/manifest 저장소 (기본).

현행 동작을 바이트 동일하게 래핑한다. manifest 정본은 ``output_root/<run_id>/manifest.json``
파일이며, ``get_manifest`` 는 기존 ``datasets_service.read_manifest`` 와 동일한 경로 안전
검사를 수행한다. 무외부의존 기본값(AGENTS.md).
"""

from __future__ import annotations

import json
from pathlib import Path

from ...stages._path_safety import ensure_within

_MANIFEST_FILENAME = "manifest.json"


class LocalArtifactStore:
    """파일시스템 기반 ArtifactStore 구현체."""

    def __init__(self, output_root: Path) -> None:
        self._output_root = output_root

    def run_dir(self, run_id: str) -> Path:
        return self._output_root / run_id

    def get_manifest(self, run_id: str) -> dict[str, object] | None:
        manifest_path = self._output_root / run_id / _MANIFEST_FILENAME
        try:
            ensure_within(self._output_root, manifest_path, label="manifest file")
        except ValueError:
            return None
        if not manifest_path.is_file():
            return None
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def put_manifest(self, run_id: str, manifest: dict[str, object]) -> None:
        run_dir = self._output_root / run_id
        manifest_path = run_dir / _MANIFEST_FILENAME
        ensure_within(self._output_root, manifest_path, label="manifest file")
        run_dir.mkdir(parents=True, exist_ok=True)
        # manifest_writer 와 동일한 결정적 직렬화(정렬 키, UTF-8, indent=2).
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def list_run_ids(self) -> list[str]:
        if not self._output_root.exists():
            return []
        return [
            d.name
            for d in self._output_root.iterdir()
            if d.is_dir() and (d / _MANIFEST_FILENAME).is_file()
        ]


__all__ = ["LocalArtifactStore"]
