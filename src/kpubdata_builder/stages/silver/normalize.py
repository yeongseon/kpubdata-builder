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

from ...errors import TabularError
from ...tabular.convert import records_to_dataframe
from ...tabular.polars_helpers import DtypeSpec, cast_columns
from ..bronze.models import BronzeArtifact


def normalize_table(
    bronze: BronzeArtifact,
    *,
    casts: Mapping[str, DtypeSpec] | None = None,
) -> pl.DataFrame:
    """Bronze raw records를 테이블로 변환하고 선언된 캐스팅만 적용한다.

    선언된 캐스팅은 ``strict=False``로 적용되므로 변환에 실패한 값이 조용히 null이
    될 수 있다. 그런 손실을 묻어두면 오염된 소스 값이 사라진 채 다운스트림 산출물이
    빌드되므로, audit으로 null 증가를 감지해 어떤 컬럼에서 몇 개가 손실됐는지 명확한
    에러로 표면화한다 (#188).

    매개변수:
        bronze: 원천 Bronze 산출물.
        casts: 컬럼별 dtype 캐스팅 규칙. None이면 캐스팅 없이 tabularize만 수행.

    반환값:
        pl.DataFrame: 정규화된 테이블.

    예외:
        TabularError: 선언된 캐스팅이 값을 null로 떨어뜨려 데이터가 손실된 경우.
    """
    table = records_to_dataframe(bronze.raw_records)
    if casts:
        result = cast_columns(table, casts, audit=True)
        if result.has_nulls_introduced:
            details = "; ".join(
                f"{report.column!r}: {report.nulls_introduced} value(s) -> null"
                for report in result.reports
                if report.nulls_introduced > 0
            )
            raise TabularError(f"declared cast dropped values to null (data loss): {details}")
        table = result.df
    return table
