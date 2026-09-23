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
from ...tabular.polars_helpers import (
    YEAR_MONTH_COMPACT,
    YEAR_MONTH_DASHED,
    DtypeSpec,
    cast_columns,
)
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
    read_as: Mapping[str, str] | None = None,
    null_tokens: Sequence[str] = (),
    column_null_tokens: Mapping[str, Sequence[str]] | None = None,
    coalesce: Mapping[str, Sequence[str]] | None = None,
    zfill: Mapping[str, int] | None = None,
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
        read_as: 원천 컬럼을 읽을 타입 선언 (``{컬럼: "str"}``). 레코드마다 타입이
            다른 원천 컬럼을 선언으로 처리한다. 키는 rename *이전* 의 원 필드명이다 —
            테이블이 만들어지기 전에 적용되기 때문이다.
        null_tokens: 결측을 나타내는 원천 표기 (예: ``("",)``). 캐스팅 전에 null로
            모은다. 결측을 지우는 것이 아니라 표기를 하나로 맞추는 것이며, 선언되지
            않은 값은 건드리지 않는다 — 무엇을 결측으로 볼지는 데이터셋마다 다르고,
            builder가 임의로 정하면 품질 측정 대상이 오염된다.
        column_null_tokens: 특정 컬럼에서만 인정하는 결측 표기 (#623). 전역
            ``null_tokens`` 에 **더해서** 적용된다 — 덮어쓰지 않는다. 키는 rename
            *이전* 의 원 필드명이고, 선언한 컬럼이 없으면 실패한다.
        coalesce: 세대별 alias 컬럼을 하나로 모으는 규칙 (#620). 키는 rename *이전*
            의 원 필드명이다. 후보 컬럼은 결과에서 사라진다.
        zfill: canonical 식별자를 선언된 폭으로 왼쪽 0 padding 한다 (#620). 키는
            rename *이후* 의 이름이다.

    적용 순서는 ``read_as -> null_tokens -> coalesce -> rename -> zfill -> casts ->
    derived`` 다. ``null_tokens`` 가 ``coalesce`` 앞이어야 하는 이유는 결측 표기가
    아직 문자열이면 coalesce가 그것을 값으로 보고 충돌시키기 때문이고, ``zfill`` 이
    ``rename`` 뒤인 이유는 선언이 canonical 이름을 가리키기 때문이다.

    반환값:
        pl.DataFrame: 정규화된 테이블.

    예외:
        TabularError: 선언된 캐스팅이 값을 null로 떨어뜨려 데이터가 손실된 경우.
    """
    table = records_to_dataframe(bronze.raw_records, read_as=read_as)
    if null_tokens or column_null_tokens:
        table = _apply_null_tokens(table, null_tokens, column_null_tokens or {})
    if coalesce:
        for target, candidates in coalesce.items():
            table = _apply_coalesce(table, target, tuple(candidates))
    if rename:
        missing = [source for source in rename if source not in table.columns]
        if missing:
            raise TabularError(
                f"declared rename refers to columns absent from the source: {missing}"
            )
        table = table.rename(dict(rename))
    if zfill:
        for column, width in zfill.items():
            table = _apply_zfill(table, column, width)
    if casts:
        _check_year_month(table, casts)
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


def _apply_null_tokens(
    table: pl.DataFrame,
    null_tokens: Sequence[str],
    column_null_tokens: Mapping[str, Sequence[str]],
) -> pl.DataFrame:
    """결측 표기를 null로 모은다. 전역 선언 + 컬럼별 선언 (#620, #623).

    한 컬럼에서 인정하는 결측 표현은 **전역 + 그 컬럼의 선언**이다. 컬럼별 선언이
    전역을 덮어쓰지 않는다 — 덮어쓰게 하면 한 컬럼에 토큰 하나를 더하려다 전역 토큰을
    잃는 사고가 조용히 난다.

    같은 의미의 결측이 컬럼마다 다르게 표기되는 원천이 있다. 전역 선언만으로는 그것을
    **다른 컬럼의 의미를 바꾸지 않고** 표현할 수 없다 — 성별의 빈 문자열을 결측으로
    선언하려다 대여소명의 빈 문자열까지 null로 만들게 된다.

    선언한 컬럼이 테이블에 없으면 실패한다. 오타가 조용한 무동작이 되면 결측이 값으로
    남은 채 품질 지표가 그것을 세지 않는다.
    """
    missing = [name for name in column_null_tokens if name not in table.columns]
    if missing:
        raise TabularError(
            f"declared column_null_tokens refers to columns absent from the source: {missing}"
        )
    # 문자열이 아닌 컬럼에서는 토큰이 맞을 수 없다. 조용히 아무것도 하지 않으면
    # 선언이 틀렸다는 것을 아무도 모른다.
    wrong_type = {
        name: str(table.schema[name])
        for name in column_null_tokens
        if table.schema[name] not in (pl.Utf8, pl.Null)
    }
    if wrong_type:
        raise TabularError(
            f"column_null_tokens declared on non-string columns: {wrong_type}. "
            "Declare read_as so the source values are read as text."
        )

    shared = list(null_tokens)
    expressions = []
    for name, dtype in table.schema.items():
        if dtype != pl.Utf8:
            continue
        extra = [token for token in column_null_tokens.get(name, ()) if token not in shared]
        tokens = shared + extra
        if not tokens:
            continue
        expressions.append(
            pl.when(pl.col(name).is_in(tokens)).then(None).otherwise(pl.col(name)).alias(name)
        )
    return table.with_columns(expressions) if expressions else table


def _apply_coalesce(table: pl.DataFrame, target: str, candidates: tuple[str, ...]) -> pl.DataFrame:
    """세대별 alias 컬럼을 하나의 canonical 컬럼으로 모은다 (#620).

    세대가 섞인 스냅샷에서는 후보 컬럼이 한 프레임 안에 동시에 존재한다 — 대부분
    한쪽이 전부 null이다. 그래서 rename이 아니라 coalesce가 필요하다.

    **수렴한 후보 컬럼은 canonical 컬럼으로 대체되어 사라진다.** "Silver는 Bronze의
    열을 보존한다"(#612)에 대한 예외로 보일 수 있으나 그렇지 않다 — 여러 세대별
    alias를 하나의 canonical field로 **수렴시키는** 연산이라, 후보는 버려지는 것이
    아니라 그 field에 흡수된다. 행은 보존되고, 사라지는 컬럼은 선언된 alias group
    안으로 한정된다. 임의의 컬럼 삭제가 아니며, ``filters`` 처럼 정보를 버리는
    연산과는 다르다.

    후보가 하나도 없으면 실패한다. 조용히 all-null 컬럼을 만들면 이후 캐스팅이
    아무것도 잃지 않은 채 통과해, 소스가 통째로 빠진 것을 아무도 모른다.
    """
    present = [name for name in candidates if name in table.columns]
    if not present:
        raise TabularError(
            f"coalesce target {target!r} found none of its candidates in the source: "
            f"{list(candidates)}"
        )
    # target이 후보가 아닌 기존 컬럼과 이름이 겹치면 그 컬럼을 말없이 덮어쓰게 된다.
    # alias group 안으로 한정된다는 계약이 거기서 깨진다.
    if target in table.columns and target not in present:
        raise TabularError(
            f"coalesce target {target!r} would overwrite an existing column that is not "
            "one of its candidates"
        )
    # 전부 null인 후보는 pl.Null로 추론된다 — 세대가 섞인 스냅샷에서 흔한 모양이고,
    # 어떤 타입과도 어긋나지 않으므로 합의 판정에서 뺀다.
    dtypes = {table.schema[name] for name in present} - {pl.Null}
    if len(dtypes) > 1:
        raise TabularError(
            f"coalesce target {target!r} has candidates of differing dtypes: "
            f"{ {name: str(table.schema[name]) for name in present} }. "
            "Declare read_as to read them as one type."
        )
    if len(present) > 1:
        # 한 행에서 non-null 후보가 둘 이상이고 값이 다르면 세대 경계가 잘못
        # 잡혔다는 뜻이다. first-wins로 삼키면 틀린 값이 표시 없이 내려간다.
        distinct = (
            pl.concat_list([pl.col(name) for name in present])
            .list.drop_nulls()
            .list.unique()
            .list.len()
        )
        conflicts = table.select(present).filter(distinct > 1)
        if conflicts.height:
            raise TabularError(
                f"coalesce target {target!r} has {conflicts.height} row(s) where candidates "
                f"disagree; first example: {conflicts.head(1).to_dicts()[0]}"
            )
    # 먼저 값을 뽑고 나서 후보를 버린다. target이 후보 중 하나와 같은 이름일 수 있어
    # drop과 with_columns의 순서를 바꾸면 방금 만든 컬럼이 도로 사라진다.
    merged = table.select(pl.coalesce([pl.col(name) for name in present]).alias(target)).to_series()
    return table.drop(present).with_columns(merged)


def _apply_zfill(table: pl.DataFrame, column: str, width: int) -> pl.DataFrame:
    """식별자의 폭을 맞춘다 (#620).

    같은 대여소가 ``3`` 과 ``00003`` 으로 오면 집계에서 둘로 갈린다. null은 null로
    둔다 — ``"00000"`` 으로 채우면 결측이 유효한 식별자가 되어 품질 지표가 세는
    결측 수가 달라진다.
    """
    if column not in table.columns:
        raise TabularError(f"declared zfill refers to a column absent from the table: {column!r}")
    if table.schema[column] != pl.Utf8:
        raise TabularError(
            f"zfill target {column!r} is {table.schema[column]}, not a string; "
            "declare read_as so the leading zeros survive reading"
        )
    # 선언 폭보다 긴 값은 자르지 않고 실패한다. 조용한 절단은 식별자를 망가뜨리고,
    # 계약이 width를 선언했는데 더 긴 값이 오는 것은 drift 신호다.
    too_long = table.filter(pl.col(column).str.len_chars() > width)
    if too_long.height:
        examples = too_long.select(column).unique().head(3).to_series().to_list()
        raise TabularError(
            f"zfill target {column!r} has {too_long.height} value(s) longer than the declared "
            f"width {width}: {examples}"
        )
    return table.with_columns(pl.col(column).str.zfill(width).alias(column))


def _check_year_month(table: pl.DataFrame, casts: Mapping[str, DtypeSpec]) -> None:
    """``year_month`` 캐스트가 거부할 값을 미리 이름 지어 보고한다 (#620).

    캐스트 자체는 맞지 않는 값을 null로 두고 #188의 audit이 개수를 센다. 개수만으로는
    무엇이 왜 거부됐는지 알 수 없고, R2는 바로 그것을 읽어야 한다.
    """
    for column, dtype in casts.items():
        if not isinstance(dtype, str) or dtype.strip().lower() != "year_month":
            continue
        if column not in table.columns:
            continue
        text = pl.col(column).cast(pl.Utf8).str.strip_chars()
        bad = table.filter(
            text.is_not_null()
            & ~text.str.contains(YEAR_MONTH_DASHED)
            & ~text.str.contains(YEAR_MONTH_COMPACT)
        )
        if bad.height:
            examples = bad.select(column).unique().head(3).to_series().to_list()
            raise TabularError(
                f"year_month cast on {column!r} rejected {bad.height} value(s); "
                f"expected YYYY-MM or YYYYMM, got: {examples}"
            )


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
