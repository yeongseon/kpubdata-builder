"""빌드 오케스트레이션: 소스 실행, 조립, 내보내기, 매니페스트를 연결한다."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .assembler import assemble_artifact
from .errors import ExportError
from .executor import SourceClient, execute_sources
from .exporters import EXPORTER_REGISTRY
from .exporters.base import BaseExporter
from .manifest import BuildManifest, manifest_writer
from .spec import BuildSpec
from .spec.validator import validate_spec

MANIFEST_FILENAME = "manifest.json"


@dataclass(frozen=True)
class BuildResult:
    """빌드 실행 결과: 생성된 파일 목록과 매니페스트 경로를 담는다."""

    artifact_paths: tuple[Path, ...]
    manifest_path: Path
    warnings: tuple[str, ...] = ()


def execute_build(
    spec: BuildSpec,
    client: SourceClient,
    *,
    output_dir: Path,
    exporters: Mapping[str, BaseExporter] = EXPORTER_REGISTRY,
    build_id: str | None = None,
) -> BuildResult:
    """검증된 스펙을 기반으로 전체 빌드 파이프라인을 실행한다.

    실행 순서: 스펙 검증 → 소스 수집 → 조립 → 내보내기 → 매니페스트 기록.
    client는 주입 방식으로 전달돼 테스트에서 네트워크 없이 가짜 구현체를 사용할 수 있다.
    매니페스트는 항상 기록되며 입력 소스, 출력 파일, 경고를 포함한다.
    """
    validate_spec(spec)

    resolved_build_id = build_id or uuid.uuid4().hex[:12]
    started_at = datetime.now(tz=timezone.utc)

    records_by_source = execute_sources(spec, client)
    assembly = assemble_artifact(spec, records_by_source)
    artifact = assembly.artifact

    artifact_paths: list[Path] = []
    for target in spec.exports:
        exporter = exporters.get(target.kind)
        if exporter is None:
            supported = ", ".join(sorted(exporters)) or "(none)"
            raise ExportError(f"Unsupported export kind: {target.kind!r}. Supported: {supported}")
        result = exporter.export(artifact, target, output_dir)
        artifact_paths.append(result.output_path)

    finished_at = datetime.now(tz=timezone.utc)
    manifest_path = output_dir / MANIFEST_FILENAME
    manifest = BuildManifest(
        build_id=resolved_build_id,
        started_at=started_at,
        finished_at=finished_at,
        inputs=tuple(f"{s.provider}.{s.dataset}" for s in spec.sources),
        outputs=tuple(str(p) for p in artifact_paths),
        warnings=assembly.warnings,
        row_counts={"records": len(artifact.records)},
    )
    manifest_writer(manifest, manifest_path)

    return BuildResult(
        artifact_paths=tuple(artifact_paths),
        manifest_path=manifest_path,
        warnings=assembly.warnings,
    )


__all__ = ["BuildResult", "execute_build", "MANIFEST_FILENAME"]
