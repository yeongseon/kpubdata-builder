"""재현 가능한 빌드 메타데이터를 위한 빌드 매니페스트 모델과 기록기.

이 모듈은 빌드 실행의 입력, 출력, 경고, 오류, 행 수 같은 감사 정보를
결정적 JSON 형식으로 보존하는 모델과 기록 함수를 제공한다.

주요 구성:
    - BuildManifest: 실행 요약 데이터 클래스
    - manifest_writer / write_manifest: 디스크 기록 함수
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .errors import ManifestError


@dataclass(frozen=True)
class BuildManifest:
    """빌드 감사를 위한 실행 요약 산출물.

    속성:
        build_id: 실행 식별자.
        started_at: 실행 시작 시각.
        finished_at: 실행 종료 시각.
        inputs: 입력 파일 또는 소스 식별자 목록.
        outputs: 생성된 결과물 경로 목록.
        warnings: 경고 메시지 목록.
        errors: 실패 또는 부분 실패 메시지 목록.
        row_counts: 단계별 또는 산출물별 레코드 수 요약.
    """

    build_id: str
    started_at: datetime
    finished_at: datetime
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    row_counts: dict[str, int] = field(default_factory=dict)


def manifest_writer(manifest: BuildManifest, output_path: Path) -> None:
    """빌드 매니페스트를 결정적 JSON으로 디스크에 기록한다.

    UTC 기준 ISO 문자열과 정렬된 키를 사용해 직렬화 결과를 안정적으로
    유지한다.

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


__all__ = ["BuildManifest", "manifest_writer", "write_manifest"]
