"""GoldPackage export plan 실행을 담당한다."""

from __future__ import annotations

from pathlib import Path

from ..artifact import ArtifactDataset
from ..exporters import get_exporter
from ..spec import JsonValue
from ..stages.gold import GoldPackage


def export_gold_package(package: GoldPackage, *, output_dir: Path) -> tuple[Path, ...]:
    """GoldPackage의 export target을 파일/디렉터리 산출물로 기록한다."""
    artifact = ArtifactDataset(
        records=tuple(_json_record(row) for row in package.table.to_dicts()),
        schema={name: str(dtype) for name, dtype in package.table.schema.items()},
        metadata=package.metadata,
        provenance=package.source_refs if package.source_refs else (package.source_silver,),
        statistics={"row_count": package.table.height},
    )
    return tuple(
        get_exporter(target.kind).export(artifact, target, output_dir).output_path
        for target in package.export_plan.targets
    )


def _json_record(row: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return dict(row)
