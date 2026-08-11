"""스키마·통계 드리프트 감지 (#445, DRIFT-1).

스케줄 워크플로가 주기적으로 재빌드할 때 소스 API가 조용히 형식을 바꿔도
감지된다. append-only 데이터셋에서는 오염이 누적되므로 직전 성공 run과
비교한다.

주요 구성:
    - DriftFinding: 단일 드리프트 관찰 (컬럼 추가/삭제/dtype 변경/행 수 급변)
    - detect_drift: 순수 함수 — 현재/이전 SchemaInfo+TableStatistics → findings
    - find_previous_silver: output_root 에서 직전 run의 silver 데이터를 찾는다
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

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


def find_previous_silver(
    output_root: Path, current_run_id: str
) -> tuple[SchemaInfo, TableStatistics] | None:
    """output_root 에서 직전 run의 silver schema/stats를 찾아 읽는다 (#445).

    현재 run 디렉터리를 제외한 가장 최근 run의 silver/schema.json +
    silver/stats.json을 읽는다. 파일이 없으면 None.
    """
    candidates = sorted(
        (
            d
            for d in output_root.iterdir()
            if d.is_dir() and d.name != current_run_id and (d / "silver" / "schema.json").exists()
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None

    prev_dir = candidates[0]
    try:
        schema_data = json.loads((prev_dir / "silver" / "schema.json").read_text(encoding="utf-8"))
        stats_data = json.loads((prev_dir / "silver" / "stats.json").read_text(encoding="utf-8"))
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
