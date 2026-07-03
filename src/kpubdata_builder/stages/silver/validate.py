"""Silver 스키마 검증 (#46).

정규화된 테이블이 선언된 스키마 요건(필수 코럼 존재, 코럼 dtype 일치)을 충족하는지 검사한다.

주요 함수:
    - validate_table: pl.DataFrame → ValidationResult
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import polars as pl

from ...tabular.polars_helpers import DtypeSpec, _resolve_dtype
from .models import ValidationResult


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
    problems: list[str] = []
    missing = [column for column in required_columns if column not in table.columns]
    if missing:
        problems.append(f"missing required columns: {', '.join(missing)}")
    for column, expected_spec in (column_dtypes or {}).items():
        if column not in table.columns:
            problems.append(f"column {column!r} not found; cannot validate dtype")
            continue
        expected = _resolve_dtype(expected_spec)
        actual = table.schema[column]
        if actual != expected:
            problems.append(
                f"column {column!r}: expected dtype {expected}, got {actual}"
            )
    return ValidationResult(ok=not problems, problems=tuple(problems))
