"""두 source의 Silver 테이블을 결합해 하나의 Gold dataset으로 조립한다 (#506).

BuildSpec.composition(JoinSpec)이 선언한 두 source를 equi-join한다. 자유형 SQL이
아니라 typed join key/type만 허용한다(#506 이슈 제외범위: 자유형 SQL transformation).

join key 존재/dtype 호환성 검증은 spec.validator(구조 검증)가 아니라 여기 — 즉
orchestrator가 호출하는 빌드 파이프라인의 런타임 게이트에서 수행한다. spec 파싱
시점에는 아직 Silver 스키마가 없어 alias/type 어휘 이상의 검증이 불가능하기
때문이다.

주요 구성:
    - CompositionError: join 실행 실패(키 부재/타입 불일치/명시적 fail 게이트)
    - CompositionStats: provenance 기록용 join 실행 통계
    - build_composed_gold_package: 두 SilverDataset → (GoldPackage, CompositionStats)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import polars as pl

from ...spec import ExportTarget, JoinSpec
from ..silver.models import SilverDataset
from .models import ExportPlan, GoldPackage

_JOIN_HOW: dict[str, Literal["inner", "left"]] = {"inner": "inner", "left": "left"}


class CompositionError(RuntimeError):
    """composition(join) 실행 실패. orchestrator가 소스 실패와 동일하게 처리한다."""


@dataclass(frozen=True)
class CompositionStats:
    """join 실행 통계 — CompositionProvenance로 그대로 옮겨진다."""

    left_row_count: int
    left_distinct_key_count: int
    right_row_count: int
    right_distinct_key_count: int
    output_row_count: int
    duplicate_key_warning: bool


def _dtypes_compatible(left: pl.DataType, right: pl.DataType) -> bool:
    """join key dtype 호환성을 판단한다.

    완전히 동일한 dtype만 호환으로 본다. Polars ``DataFrame.join``은 Int64/Float64처럼
    "숫자형끼리도" 암묵적으로 캐스팅해 주지 않고 SchemaError로 거부한다 — 여기서 느슨하게
    허용해도 실제 join 시점에 다른 오류로 실패할 뿐이므로 미리 정확히 같은 실패를
    명확한 메시지로 낸다. 타입을 맞추려면 자유형 캐스팅 대신 기존
    ``sources[].schema.casts`` 계약(#437)으로 Silver 단계에서 명시적으로 정규화한다.
    """
    return left == right


def _validate_join_keys(
    left_table: pl.DataFrame, right_table: pl.DataFrame, join: JoinSpec
) -> None:
    """join key 존재와 dtype 호환성을 확인한다. 빌드 파이프라인의 런타임 검증 게이트다."""
    if join.left_key not in left_table.columns:
        raise CompositionError(
            f"composition.join.left_key {join.left_key!r} not found in {join.left!r} columns: "
            f"{sorted(left_table.columns)}"
        )
    if join.right_key not in right_table.columns:
        raise CompositionError(
            f"composition.join.right_key {join.right_key!r} not found in {join.right!r} columns: "
            f"{sorted(right_table.columns)}"
        )
    left_dtype = left_table.schema[join.left_key]
    right_dtype = right_table.schema[join.right_key]
    if not _dtypes_compatible(left_dtype, right_dtype):
        raise CompositionError(
            f"composition join key dtype mismatch: {join.left}.{join.left_key} ({left_dtype}) "
            f"vs {join.right}.{join.right_key} ({right_dtype})"
        )


def _distinct_key_count(table: pl.DataFrame, key: str) -> int:
    """null을 제외한 join key의 distinct 값 수. null은 어느 표준에서도 서로 일치하지 않는다."""
    return int(table[key].drop_nulls().n_unique())


def build_composed_gold_package(
    *,
    left_silver: SilverDataset,
    right_silver: SilverDataset,
    join: JoinSpec,
    dataset_name: str,
    exports: Sequence[ExportTarget] = (),
    metadata: Mapping[str, str] | None = None,
) -> tuple[GoldPackage, CompositionStats]:
    """두 SilverDataset을 join해 결합 GoldPackage와 실행 통계를 만든다.

    매개변수:
        left_silver: join.left에 대응하는 Silver 데이터셋.
        right_silver: join.right에 대응하는 Silver 데이터셋.
        join: 결합 계약.
        dataset_name: 결합 결과 Gold dataset 이름 (CompositionSpec.name).
        exports: 내보내기 대상 목록 (BuildSpec.exports 재사용).
        metadata: 패키지에 실을 메타데이터.

    반환값:
        (GoldPackage, CompositionStats): 결합 패키지와 provenance 기록용 통계.

    예외:
        CompositionError: join key가 없거나 dtype이 호환되지 않는 경우, 또는
            ``join.on_duplicate_key == "fail"``인데 many-to-many 중복 키가
            감지된 경우.
    """
    left_table = left_silver.table
    right_table = right_silver.table
    _validate_join_keys(left_table, right_table, join)

    left_row_count = left_table.height
    right_row_count = right_table.height
    left_distinct = _distinct_key_count(left_table, join.left_key)
    right_distinct = _distinct_key_count(right_table, join.right_key)
    # 양쪽 join key가 모두 중복 값을 가지면 many-to-many가 되어 행이 곱셈으로
    # 폭증할 수 있다 — 정확한 배수보다 이 구조적 신호(양쪽 다 non-unique)를 감지한다.
    duplicate_key_warning = left_distinct < left_row_count and right_distinct < right_row_count

    if duplicate_key_warning and join.on_duplicate_key == "fail":
        raise CompositionError(
            f"composition {dataset_name!r}: duplicate join keys on both sides "
            f"({join.left}.{join.left_key}: {left_row_count - left_distinct} duplicate rows, "
            f"{join.right}.{join.right_key}: {right_row_count - right_distinct} duplicate rows) "
            "would multiply output rows (on_duplicate_key='fail')"
        )

    combined = left_table.join(
        right_table,
        left_on=join.left_key,
        right_on=join.right_key,
        how=_JOIN_HOW[join.type],
        suffix=f"_{join.right}",
    )

    stats = CompositionStats(
        left_row_count=left_row_count,
        left_distinct_key_count=left_distinct,
        right_row_count=right_row_count,
        right_distinct_key_count=right_distinct,
        output_row_count=combined.height,
        duplicate_key_warning=duplicate_key_warning,
    )
    package = GoldPackage(
        dataset_name=dataset_name,
        table=combined,
        export_plan=ExportPlan(targets=tuple(exports)),
        source_silver=f"{join.left}+{join.right}",
        metadata=dict(metadata or {}),
        source_refs=(join.left, join.right),
    )
    return package, stats


__all__ = ["CompositionError", "CompositionStats", "build_composed_gold_package"]
