"""재현 가능한 빌드 메타데이터를 위한 빌드 매니페스트 모델과 기록기."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .errors import ManifestError


@dataclass(frozen=True)
class BuildManifest:
    """빌드 감사를 위한 실행 요약 산출물."""

    build_id: str
    started_at: datetime
    finished_at: datetime
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    row_counts: dict[str, int] = field(default_factory=dict)


def manifest_writer(manifest: BuildManifest, output_path: Path) -> None:
    """빌드 매니페스트를 결정적 JSON으로 디스크에 기록한다."""
    payload = {
        "build_id": manifest.build_id,
        "started_at": manifest.started_at.astimezone(timezone.utc).isoformat(),
        "finished_at": manifest.finished_at.astimezone(timezone.utc).isoformat(),
        "inputs": list(manifest.inputs),
        "outputs": list(manifest.outputs),
        "warnings": list(manifest.warnings),
        "errors": list(manifest.errors),
        "row_counts": manifest.row_counts,
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _ = output_path.write_text(f"{serialized}\n", encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"Failed to write manifest to {output_path}: {exc}") from exc


def write_manifest(manifest: BuildManifest, output_path: Path) -> None:
    """빌드 매니페스트를 결정적 JSON으로 디스크에 기록한다."""
    manifest_writer(manifest, output_path)


__all__ = ["BuildManifest", "manifest_writer", "write_manifest"]
