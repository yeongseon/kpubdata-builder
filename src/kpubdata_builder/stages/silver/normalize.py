"""Silver 정규화 (#46).

Bronze raw records를 Polars 테이블로 변환하고, spec에 선언된 정규화 규칙(타입
캐스팅)만 적용한다. Builder가 임의로 "깨끗한 데이터"를 정의하지 않는다 —
선언되지 않은 변형은 수행하지 않는다.

주요 함수:
    - normalize_table: BronzeArtifact → pl.DataFrame
"""

from __future__ import annotations

from collections.abc import Mapping

import polars as pl

from ...tabular.convert import records_to_dataframe
from ...tabular.polars_helpers import DtypeSpec, cast_columns
from ..bronze.models import BronzeArtifact


def normalize_table(
    bronze: BronzeArtifact,
    *,
    casts: Mapping[str, DtypeSpec] | None = None,
) -> pl.DataFrame:
    """Bronze raw records를 테이블로 변환하고 선언된 캐스팅만 적용한다.

    매개변수:
        bronze: 원천 Bronze 산출물.
        casts: 컬럼별 dtype 캐스팅 규칙. None이면 캐스팅 없이 tabularize만 수행.

    반환값:
        pl.DataFrame: 정규화된 테이블.
    """
    table = records_to_dataframe(bronze.raw_records)
    if casts:
        table = cast_columns(table, casts)
    return table
