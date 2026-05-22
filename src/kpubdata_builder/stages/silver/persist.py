"""Persist Silver stage datasets to a run workspace."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from ...spec import JsonValue
from .models import SilverDataset

_SAFE_PATH_SEGMENT = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


@dataclass(frozen=True)
class SilverPersistResult:
    """Filesystem paths written for a Silver dataset."""

    silver_dir: Path
    table_path: Path
    schema_path: Path
    stats_path: Path
    preview_path: Path


def _validate_path_segment(value: str, *, field_name: str) -> None:
    """Reject path segments that could escape the workspace."""
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{field_name} must not have leading/trailing whitespace")
    if not _SAFE_PATH_SEGMENT.match(value):
        raise ValueError(
            f"{field_name} contains unsafe characters: {value!r}. "
            "Only alphanumeric, dot, hyphen, and underscore are allowed."
        )


def _write_json(path: Path, payload: dict[str, JsonValue]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def persist_silver_dataset(
    dataset: SilverDataset,
    *,
    output_root: Path,
    run_id: str,
) -> SilverPersistResult:
    """Write the table (parquet) and schema/stats/preview (json) under
    build/{run_id}/silver/{source_bronze}.

    Raises ValueError if run_id or source_bronze contain unsafe path characters.
    """
    _validate_path_segment(run_id, field_name="run_id")
    source_segment = dataset.source_bronze.replace("/", "_")
    _validate_path_segment(source_segment, field_name="source_bronze")

    silver_dir = output_root / run_id / "silver" / source_segment
    table_path = silver_dir / "table.parquet"
    schema_path = silver_dir / "schema.json"
    stats_path = silver_dir / "stats.json"
    preview_path = silver_dir / "preview.json"

    resolved_root = output_root.resolve()
    resolved_silver = silver_dir.resolve()
    if not str(resolved_silver).startswith(str(resolved_root)):
        raise ValueError(
            f"Resolved silver directory {resolved_silver} escapes output_root {resolved_root}"
        )

    silver_dir.mkdir(parents=True, exist_ok=True)

    dataset.table.write_parquet(table_path)
    _write_json(schema_path, asdict(dataset.schema_summary))
    _write_json(stats_path, asdict(dataset.statistics))
    _write_json(preview_path, asdict(dataset.preview))

    return SilverPersistResult(
        silver_dir=silver_dir,
        table_path=table_path,
        schema_path=schema_path,
        stats_path=stats_path,
        preview_path=preview_path,
    )
