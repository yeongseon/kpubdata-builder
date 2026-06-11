"""dict ↔ Polars 변환 유틸 (#49).

원시 JSON 유사 레코드와 Polars DataFrame 사이의 양방향 변환을 담당한다.
공개 API에 Polars 타입을 강제하지 않도록 records 측은 plain dict를 사용한다.

주요 함수:
    - records_to_dataframe: 레코드 시퀀스 → pl.DataFrame
    - dataframe_to_records: pl.DataFrame → plain dict 리스트
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

import polars as pl

from ..errors import TabularError
from ..spec import JsonValue


def _value_category(value: object) -> str:
    """값을 호환 그룹으로 분류한다(혼합 타입 컬럼 감지용).

    int/float는 수치로 함께 묶지만 bool은 별도로 둔다. JSON에서 의미가 다른 타입이
    한 컬럼에 섞이면(예: 문자열과 숫자) Polars 자동 추론이 조용히 한쪽으로 강제
    변환하므로, 그런 경우를 감지하기 위한 분류다.
    """
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "mapping"
    if isinstance(value, (list, tuple)):
        return "list"
    return type(value).__name__


def records_to_dataframe(records: Sequence[dict[str, JsonValue]]) -> pl.DataFrame:
    """원시 레코드 매핑을 Polars DataFrame으로 변환한다.

    Polars 자동 추론에만 의존하면 혼합 타입 컬럼이 검증 전에 조용히 강제 변환되어
    원시 값이 바뀔 수 있다. 그래서 변환 전에 컬럼별로 호환되지 않는 타입이 섞였는지
    먼저 감지하고, 발견되면 명확한 에러로 빠르게 실패한다 (#187).

    매개변수:
        records: JSON 호환 레코드 시퀀스.

    반환값:
        pl.DataFrame: 입력 레코드의 컬럼 구조를 반영한 DataFrame.

    예외:
        TabularError: 한 컬럼에 호환되지 않는 타입(예: 문자열+숫자)이 섞인 경우.
    """
    categories: dict[str, set[str]] = {}
    for record in records:
        for key, value in record.items():
            if value is None:
                continue
            categories.setdefault(key, set()).add(_value_category(value))

    heterogeneous = {col: cats for col, cats in categories.items() if len(cats) > 1}
    if heterogeneous:
        details = "; ".join(
            f"{col!r}: {', '.join(sorted(cats))}" for col, cats in sorted(heterogeneous.items())
        )
        raise TabularError(
            f"heterogeneous column types detected (refusing to silently coerce): {details}"
        )

    return pl.DataFrame(list(records))


def dataframe_to_records(df: pl.DataFrame) -> list[dict[str, JsonValue]]:
    """Polars DataFrame을 plain dict 레코드 리스트로 변환한다.

    매개변수:
        df: 변환할 DataFrame.

    반환값:
        list[dict[str, JsonValue]]: 행별 dict 표현.
    """
    return cast(list[dict[str, JsonValue]], df.to_dicts())


__all__ = [
    "dataframe_to_records",
    "records_to_dataframe",
]
