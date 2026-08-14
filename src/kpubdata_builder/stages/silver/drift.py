"""스키마·통계 드리프트 감지 (#445, DRIFT-1; dataset/source 범위 한정은 #486).

스케줄 워크플로가 주기적으로 재빌드할 때 소스 API가 조용히 형식을 바꿔도
감지된다. append-only 데이터셋에서는 오염이 누적되므로 직전 성공 run과
비교한다.

주요 구성:
    - DriftFinding: 단일 드리프트 관찰 (컬럼 추가/삭제/dtype 변경/행 수 급변)
    - detect_drift: 순수 함수 — 현재/이전 SchemaInfo+TableStatistics → findings
    - find_previous_silver: 동일 dataset_id·source_key의 직전 "성공" run에서 silver
      데이터를 찾는다 (#486) — 다른 dataset/source의 silver와 비교해 가짜 drift를
      만들지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from ...spec.serializer import BUILDSPEC_SNAPSHOT_FILENAME
from ...tabular import SchemaInfo, TableStatistics
from ...tabular.types import ColumnInfo

# 행 수 급변 임계 (직전 대비 50% 이상 변화).
_ROW_COUNT_CHANGE_THRESHOLD = 0.5


@dataclass(frozen=True)
class DriftFinding:
    """단일 드리프트 관찰.

    속성:
        kind: column_added | column_removed | dtype_changed | row_count_jump.
        column: 관련 컬럼명. 테이블 전체 문제면 None.
        detail: 사람이 읽는 설명.
    """

    kind: str
    column: str | None
    detail: str


def detect_drift(
    current_schema: SchemaInfo,
    current_stats: TableStatistics,
    previous_schema: SchemaInfo,
    previous_stats: TableStatistics,
) -> list[DriftFinding]:
    """현재/이전 스키마·통계를 비교해 드리프트를 감지한다 (#445).

    순수 함수 — 파일 I/O 없이 데이터만 받아 비교한다.
    """
    findings: list[DriftFinding] = []
    current_cols = {c.name: c for c in current_schema.columns}
    previous_cols = {c.name: c for c in previous_schema.columns}

    # 컬럼 추가.
    for name in sorted(current_cols.keys() - previous_cols.keys()):
        findings.append(DriftFinding(kind="column_added", column=name, detail="new column"))
    # 컬럼 삭제.
    for name in sorted(previous_cols.keys() - current_cols.keys()):
        findings.append(DriftFinding(kind="column_removed", column=name, detail="column gone"))
    # dtype 변경.
    for name in sorted(current_cols.keys() & previous_cols.keys()):
        if current_cols[name].dtype != previous_cols[name].dtype:
            findings.append(
                DriftFinding(
                    kind="dtype_changed",
                    column=name,
                    detail=f"{previous_cols[name].dtype} → {current_cols[name].dtype}",
                )
            )

    # 행 수 급변.
    if previous_stats.row_count > 0:
        change = abs(current_stats.row_count - previous_stats.row_count) / previous_stats.row_count
        if change > _ROW_COUNT_CHANGE_THRESHOLD:
            findings.append(
                DriftFinding(
                    kind="row_count_jump",
                    column=None,
                    detail=f"{previous_stats.row_count} → {current_stats.row_count} ({change:.0%})",
                )
            )

    return findings


def _run_dataset_id(run_dir: Path) -> str | None:
    """run_dir의 buildspec.yaml snapshot에서 dataset_id만 가볍게 읽는다.

    service/datasets.py의 read_snapshot_dataset_id와 동일한 의도지만, silver
    stage는 service 계층에 의존할 수 없으므로(레이어링) 여기서 최소한으로
    다시 구현한다. snapshot이 없거나 읽을 수 없으면 None — 추측하지 않는다.
    """
    snapshot_path = run_dir / BUILDSPEC_SNAPSHOT_FILENAME
    if not snapshot_path.is_file():
        return None
    try:
        doc = yaml.safe_load(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(doc, dict):
        return None
    dataset_id = doc.get("dataset_id")
    return dataset_id if isinstance(dataset_id, str) and dataset_id else None


def _run_succeeded(run_dir: Path) -> tuple[bool, str]:
    """(성공 여부, finished_at 정렬키)를 manifest.json에서 읽는다.

    manifest가 없거나 손상되었거나 errors가 있으면 실패로 취급한다 — 실패/부분
    실패 run의 silver는 drift 비교 대상에서 제외한다(#486).
    """
    manifest_path = run_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False, ""
    if not isinstance(manifest, dict) or manifest.get("errors"):
        return False, ""
    finished_at = manifest.get("finished_at")
    return True, finished_at if isinstance(finished_at, str) else ""


def find_previous_silver(
    output_root: Path,
    current_run_id: str,
    *,
    dataset_id: str,
    source_key: str,
) -> tuple[SchemaInfo, TableStatistics] | None:
    """동일 dataset_id·source_key의 직전 "성공" run에서 silver schema/stats를 찾는다.

    "직전 아무 run"과 비교하지 않는다(#486) — 다음 조건을 모두 만족하는 run 중
    finished_at 기준 가장 최근 것을 선택한다:
        - buildspec.yaml snapshot의 dataset_id가 정확히 일치
        - manifest.json에 errors가 없음(성공 run)
        - 해당 source_key의 silver/schema.json + silver/stats.json이 존재

    조건을 만족하는 run이 없으면 None(비교 대상 없음 — drift "없음"이 아니라
    "평가 불가"다. 호출자는 이 경우 drift 감지를 건너뛴다).
    """
    if not output_root.exists():
        return None
    source_segment = source_key.replace("/", "_")
    candidates: list[tuple[str, str, Path]] = []
    for run_dir in output_root.iterdir():
        if not run_dir.is_dir() or run_dir.name == current_run_id:
            continue
        if _run_dataset_id(run_dir) != dataset_id:
            continue
        succeeded, sort_key = _run_succeeded(run_dir)
        if not succeeded:
            continue
        silver_dir = run_dir / "silver" / source_segment
        if not (silver_dir / "schema.json").is_file() or not (silver_dir / "stats.json").is_file():
            continue
        candidates.append((sort_key, run_dir.name, silver_dir))
    if not candidates:
        return None

    # finished_at 내림차순, 동일 시각은 run_id 내림차순으로 결정적 타이브레이크
    # (#488 sort_key semantics와 동일한 원칙).
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    _, _, prev_silver_dir = candidates[0]
    try:
        schema_data = json.loads((prev_silver_dir / "schema.json").read_text(encoding="utf-8"))
        stats_data = json.loads((prev_silver_dir / "stats.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    columns = tuple(
        ColumnInfo(
            name=c.get("name", ""),
            dtype=c.get("dtype", ""),
            nullable=c.get("nullable", True),
            unique_count=c.get("unique_count", 0),
        )
        for c in schema_data.get("columns", [])
    )
    schema = SchemaInfo(columns=columns)
    stats = TableStatistics(
        row_count=stats_data.get("row_count", 0),
        null_counts=stats_data.get("null_counts", {}),
        duplicate_rate=stats_data.get("duplicate_rate", 0.0),
    )
    return schema, stats


__all__ = ["DriftFinding", "detect_drift", "find_previous_silver"]
