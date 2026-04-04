"""Build manifest model and writer for reproducible build metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class BuildManifest:
    """Execution summary artifact for build auditing."""

    build_id: str
    started_at: datetime
    finished_at: datetime
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    row_counts: dict[str, int] = field(default_factory=dict)


def manifest_writer(manifest: BuildManifest, output_path: Path) -> None:
    """Write a build manifest as deterministic JSON to disk."""
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
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _ = output_path.write_text(f"{serialized}\n", encoding="utf-8")
