"""dict ↔ Polars 변환 유틸 (#49).

원시 JSON 유사 레코드와 Polars DataFrame 사이의 양방향 변환을 담당한다.
공개 API에 Polars 타입을 강제하지 않도록 records 측은 plain dict를 사용한다.

주요 함수:
    - records_to_dataframe: 레코드 시퀀스 → pl.DataFrame
    - dataframe_to_records: pl.DataFrame → plain dict 리스트
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

import polars as pl

from ..spec import JsonValue


def records_to_dataframe(records: Sequence[dict[str, JsonValue]]) -> pl.DataFrame:
    """원시 레코드 매핑을 Polars DataFrame으로 변환한다.

    매개변수:
        records: JSON 호환 레코드 시퀀스.

    반환값:
        pl.DataFrame: 입력 레코드의 컬럼 구조를 반영한 DataFrame.
    """
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
