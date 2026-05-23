"""Gold 단계 산출물을 실행 워크스페이스에 저장한다 (#47).

GoldPackage를 build/{run_id}/gold/{dataset_name}/ 아래에 저장한다. 테이블은
parquet으로, 패키지 메타데이터는 결정적 JSON으로 기록한다.

주요 구성:
    - GoldPersistResult: 저장 경로 결과 객체
    - persist_gold_package: Gold 산출물 파일 기록 함수
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ...spec import JsonValue
from .models import GoldPackage

_SAFE_PATH_SEGMENT = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


@dataclass(frozen=True)
class GoldPersistResult:
    """Gold 산출물을 위해 기록된 파일시스템 경로.

    속성:
        gold_dir: 산출물이 저장된 Gold 디렉터리.
        table_path: 최종 테이블 parquet 파일 경로.
        package_path: 패키지 메타데이터 JSON 경로.
    """

    gold_dir: Path
    table_path: Path
    package_path: Path


def _validate_path_segment(value: str, *, field_name: str) -> None:
    """워크스페이스를 벗어날 수 있는 경로 세그먼트를 거부한다."""
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{field_name} must not have leading/trailing whitespace")
    if not _SAFE_PATH_SEGMENT.match(value):
        raise ValueError(
            f"{field_name} contains unsafe characters: {value!r}. "
            "Only alphanumeric, dot, hyphen, and underscore are allowed."
        )


def _package_metadata(package: GoldPackage) -> dict[str, JsonValue]:
    """GoldPackage의 JSON 직렬화 가능한 메타데이터를 구성한다."""
    return {
        "dataset_name": package.dataset_name,
        "source_silver": package.source_silver,
        "row_count": package.table.height,
        "columns": cast(list[JsonValue], list(package.table.columns)),
        "metadata": dict(package.metadata),
        "export_plan": {
            "targets": [
                {
                    "kind": target.kind,
                    "output_path": target.output_path,
                    "options": target.options,
                }
                for target in package.export_plan.targets
            ],
        },
    }


def persist_gold_package(
    package: GoldPackage,
    *,
    output_root: Path,
    run_id: str,
) -> GoldPersistResult:
    """Gold 산출물을 build/{run_id}/gold/{dataset_name}/ 아래에 기록한다.

    run_id 또는 dataset_name에 안전하지 않은 경로 문자가 포함되면 ValueError를
    발생시킨다.

    매개변수:
        package: 저장할 Gold 패키지.
        output_root: 실행 워크스페이스 루트.
        run_id: 빌드 실행 식별자.

    반환값:
        GoldPersistResult: 기록된 파일 경로 모음.
    """
    _validate_path_segment(run_id, field_name="run_id")
    _validate_path_segment(package.dataset_name, field_name="dataset_name")

    gold_dir = output_root / run_id / "gold" / package.dataset_name

    # 최종 포함 여부 검사: 해석된 경로는 반드시 output_root 아래에 있어야 한다.
    resolved_root = output_root.resolve()
    resolved_gold = gold_dir.resolve()
    if not str(resolved_gold).startswith(str(resolved_root)):
        raise ValueError(
            f"Resolved gold directory {resolved_gold} escapes output_root {resolved_root}"
        )

    gold_dir.mkdir(parents=True, exist_ok=True)

    table_path = gold_dir / "table.parquet"
    package_path = gold_dir / "package.json"

    package.table.write_parquet(table_path)
    package_path.write_text(
        json.dumps(_package_metadata(package), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return GoldPersistResult(
        gold_dir=gold_dir,
        table_path=table_path,
        package_path=package_path,
    )
