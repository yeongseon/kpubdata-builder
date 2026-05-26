"""빌드 매니페스트 기록기 (Medallion 재구성: 기존 manifest.py에서 분리).

이 모듈은 BuildManifest를 결정적 JSON으로 디스크에 기록한다. UTC 기준 ISO
문자열과 정렬된 키를 사용해 직렬화 결과를 안정적으로 유지한다.

주요 함수:
    - manifest_writer / write_manifest: 디스크 기록 함수
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import timezone
from pathlib import Path

from ..errors import ManifestError
from .models import BuildManifest


def manifest_writer(manifest: BuildManifest, output_path: Path) -> None:
    """빌드 매니페스트를 결정적 JSON으로 디스크에 기록한다.

    매개변수:
        manifest: 기록할 매니페스트 데이터.
        output_path: 결과 JSON 파일 경로.

    예외:
        ManifestError: 디렉터리 생성 또는 파일 기록에 실패한 경우.
    """
    payload = {
        "build_id": manifest.build_id,
        "started_at": manifest.started_at.astimezone(timezone.utc).isoformat(),
        "finished_at": manifest.finished_at.astimezone(timezone.utc).isoformat(),
        "inputs": list(manifest.inputs),
        "outputs": list(manifest.outputs),
        "warnings": list(manifest.warnings),
        "errors": list(manifest.errors),
        "row_counts": manifest.row_counts,
        "schema_summaries": {
            key: asdict(summary) for key, summary in manifest.schema_summaries.items()
        },
        "provenance": [asdict(entry) for entry in manifest.provenance],
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _ = output_path.write_text(f"{serialized}\n", encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"Failed to write manifest to {output_path}: {exc}") from exc


def write_manifest(manifest: BuildManifest, output_path: Path) -> None:
    """빌드 매니페스트를 기록하는 공개 별칭 함수다.

    매개변수:
        manifest: 기록할 매니페스트 객체.
        output_path: 출력 경로.
    """
    manifest_writer(manifest, output_path)


__all__ = ["manifest_writer", "write_manifest"]
