"""Silver 정규화 (#46).

Bronze raw records를 Polars 테이블로 변환하고, spec에 선언된 정규화 규칙(타입
캐스팅)만 적용한다. Builder가 임의로 "깨끗한 데이터"를 정의하지 않는다 —
선언되지 않은 변형은 수행하지 않는다.

주요 함수:
    - normalize_table: BronzeArtifact → pl.DataFrame
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import polars as pl

from ...errors import TabularError
from ...spec import DerivedColumn
from ...tabular.convert import records_to_dataframe
from ...tabular.polars_helpers import DtypeSpec, cast_columns
from ..bronze.models import BronzeArtifact

#: join_key 파생 컬럼의 구분자 (#611). 값에 나타나지 않는 문자를 써서 서로 다른
#: 키 조합이 같은 문자열로 합쳐지지 않게 한다.
JOIN_KEY_SEPARATOR = "|"


def normalize_table(
    bronze: BronzeArtifact,
    *,
    casts: Mapping[str, DtypeSpec] | None = None,
    rename: Mapping[str, str] | None = None,
    derived: Sequence[DerivedColumn] = (),
) -> pl.DataFrame:
    """Bronze raw records를 테이블로 변환하고 선언된 캐스팅만 적용한다.

    선언된 캐스팅은 ``strict=False``로 적용되므로 변환에 실패한 값이 조용히 null이
    될 수 있다. 그런 손실을 묻어두면 오염된 소스 값이 사라진 채 다운스트림 산출물이
    빌드되므로, audit으로 null 증가를 감지해 어떤 컬럼에서 몇 개가 손실됐는지 명확한
    에러로 표면화한다 (#188).

    매개변수:
        bronze: 원천 Bronze 산출물.
        casts: 컬럼별 dtype 캐스팅 규칙. None이면 캐스팅 없이 tabularize만 수행.
            키는 rename 적용 *후* 의 컬럼명이다.
        rename: 원 필드명 → canonical 컬럼명 매핑 (#611). 캐스팅보다 먼저 적용된다.
        derived: 기존 컬럼에서 새 컬럼을 만드는 규칙 (#611). 캐스팅 뒤에 적용된다 —
            파생 규칙이 캐스팅된 값을 읽을 수 있어야 하기 때문이다.

    반환값:
        pl.DataFrame: 정규화된 테이블.

    예외:
        TabularError: 선언된 캐스팅이 값을 null로 떨어뜨려 데이터가 손실된 경우.
    """
    table = records_to_dataframe(bronze.raw_records)
    if rename:
        missing = [source for source in rename if source not in table.columns]
        if missing:
            raise TabularError(
                f"declared rename refers to columns absent from the source: {missing}"
            )
        table = table.rename(dict(rename))
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
    for rule in derived:
        table = _apply_derived(table, rule)
    return table


def _apply_derived(table: pl.DataFrame, rule: DerivedColumn) -> pl.DataFrame:
    """파생 규칙 하나를 적용한다 (#611)."""
    missing = [column for column in rule.columns if column not in table.columns]
    if missing:
        raise TabularError(
            f"derived column {rule.name!r} refers to columns absent from the table: {missing}"
        )
    if rule.kind == "date_parts":
        year, month, day = rule.columns
        composed = (
            pl.col(year).cast(pl.Utf8).str.zfill(4)
            + pl.lit("-")
            + pl.col(month).cast(pl.Utf8).str.zfill(2)
            + pl.lit("-")
            + pl.col(day).cast(pl.Utf8).str.zfill(2)
        )
        return table.with_columns(composed.str.to_date("%Y-%m-%d", strict=False).alias(rule.name))
    if rule.kind == "join_key":
        # 키 컬럼 중 하나라도 null이면 결과도 null이다(concat_str 기본 동작) —
        # 조인 키를 만들 수 없는 행을 빈 문자열로 붙여 만들어내지 않는다.
        composed_key = pl.concat_str(
            [pl.col(column).cast(pl.Utf8) for column in rule.columns],
            separator=JOIN_KEY_SEPARATOR,
        )
        return table.with_columns(composed_key.alias(rule.name))
    raise TabularError(f"unsupported derived column kind: {rule.kind!r}")
