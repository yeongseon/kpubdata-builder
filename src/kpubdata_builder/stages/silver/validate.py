"""Silver 스키마 검증 (#46).

정규화된 테이블이 선언된 스키마 요건(필수 코럼 존재, 코럼 dtype 일치)을 충족하는지 검사한다.

주요 타입:
    - ValidationProblem: 개별 검증 위반 사항을 구조화한 객체 (#261)
    - ValidationResult: 검증 결과 (ok + problems)

주요 함수:
    - validate_table: pl.DataFrame → ValidationResult
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import polars as pl

from ...tabular.polars_helpers import DtypeSpec, _resolve_dtype
from .models import ValidationResult


@dataclass(frozen=True)
class ValidationProblem:
    """개별 검증 위반 사항 (#261).

    속성:
        code: 오류 종류를 나타내는 코드 (예: "missing_column", "dtype_mismatch").
        field: 관련 컬럼명. 테이블 전체 문제일 경우 None.
        message: 사람이 읽는 한국어 설명.
    """

    code: str
    field: str | None
    message: str


def validate_table(
    table: pl.DataFrame,
    *,
    required_columns: Sequence[str] = (),
    column_dtypes: Mapping[str, DtypeSpec] | None = None,
) -> ValidationResult:
    """필수 코럼 존재 여부와 선언된 dtype 일치를 검증한다.

    매개변수:
        table: 검증할 테이블.
        required_columns: 반드시 존재해야 하는 코럼 목록.
        column_dtypes: 코럼별 기대 dtype. 키는 코럼명,
            값은 DtypeSpec(str | pl.DataType | type[pl.DataType]).
            코럼이 없으면 해당 코럼에 대한 dtype 문제를 등록한다.

    반환값:
        ValidationResult: 통과 여부와 위반 메시지.
    """
    problems: list[ValidationProblem] = []
    missing = [column for column in required_columns if column not in table.columns]
    if missing:
        for col in missing:
            problems.append(
                ValidationProblem(
                    code="missing_column",
                    field=col,
                    message=f"필수 컬럼 누락: {col}",
                )
            )
    for column, expected_spec in (column_dtypes or {}).items():
        if column not in table.columns:
            problems.append(
                ValidationProblem(
                    code="dtype_mismatch",
                    field=column,
                    message=f"컬럼 {column!r} 없음; dtype 검증 불가",
                )
            )
            continue
        expected = _resolve_dtype(expected_spec)
        actual = table.schema[column]
        if actual != expected:
            problems.append(
                ValidationProblem(
                    code="dtype_mismatch",
                    field=column,
                    message=f"컬럼 {column!r}: 예상 dtype {expected}, 실제 {actual}",
                )
            )
    return ValidationResult(ok=not problems, problems=tuple(problems))
