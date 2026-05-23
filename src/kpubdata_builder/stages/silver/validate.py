"""Silver 스키마 검증 (#46).

정규화된 테이블이 선언된 스키마 요건(필수 컬럼 존재)을 충족하는지 검사한다.

주요 함수:
    - validate_table: pl.DataFrame → ValidationResult
"""

from __future__ import annotations

from collections.abc import Sequence

import polars as pl

from .models import ValidationResult


def validate_table(
    table: pl.DataFrame,
    *,
    required_columns: Sequence[str] = (),
) -> ValidationResult:
    """필수 컬럼 존재 여부를 검증한다.

    매개변수:
        table: 검증할 테이블.
        required_columns: 반드시 존재해야 하는 컬럼 목록.

    반환값:
        ValidationResult: 통과 여부와 위반 메시지.
    """
    problems: list[str] = []
    missing = [column for column in required_columns if column not in table.columns]
    if missing:
        problems.append(f"missing required columns: {', '.join(missing)}")
    return ValidationResult(ok=not problems, problems=tuple(problems))
