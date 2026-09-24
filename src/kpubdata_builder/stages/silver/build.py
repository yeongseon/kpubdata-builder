"""Silver 단계 오케스트레이션 (#46).

BronzeArtifact를 받아 normalize → validate → summarize → preview 순서로
처리하고 SilverDataset으로 조립한다.

주요 함수:
    - build_silver_dataset: BronzeArtifact → SilverDataset
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ...spec import DerivedColumn
from ...tabular import DEFAULT_PREVIEW_LIMIT
from ...tabular.polars_helpers import DtypeSpec
from ..bronze.models import BronzeArtifact
from .models import SilverDataset
from .normalize import normalize_table
from .preview import build_preview
from .summarize import build_schema, build_statistics
from .validate import validate_table


def build_silver_dataset(
    bronze: BronzeArtifact,
    *,
    required_columns: Sequence[str] = (),
    casts: Mapping[str, DtypeSpec] | None = None,
    rename: Mapping[str, str] | None = None,
    derived: Sequence[DerivedColumn] = (),
    read_as: Mapping[str, str] | None = None,
    null_tokens: Sequence[str] = (),
    coalesce: Mapping[str, Sequence[str]] | None = None,
    zfill: Mapping[str, int] | None = None,
    column_dtypes: Mapping[str, DtypeSpec] | None = None,
    preview_limit: int = DEFAULT_PREVIEW_LIMIT,
) -> SilverDataset:
    """Bronze 산출물을 Silver 데이터셋으로 변환한다.

    매개변수:
        bronze: 원천 Bronze 산출물.
        required_columns: 검증에 사용할 필수 컬럼 목록.
        casts: 정규화 시 적용할 컬럼별 dtype 캐스팅 규칙.
        rename: 원 필드명 → canonical 컬럼명 매핑 (#611).
        derived: 기존 컬럼에서 새 컬럼을 만드는 규칙 (#611).
        read_as: 원천 컬럼을 읽을 타입 선언.
        null_tokens: 결측을 나타내는 원천 표기.
        coalesce: 세대별 alias 컬럼을 하나로 모으는 규칙 (#620).
        zfill: canonical 식별자를 선언된 폭으로 채우는 규칙 (#620).
        column_dtypes: 검증에 사용할 코럼별 기대 dtype 규칙. 키는 코럼명,
            값은 DtypeSpec(str | pl.DataType | type[pl.DataType]).
        preview_limit: 미리보기에 포함할 최대 행 수.

    반환값:
        SilverDataset: 정제 테이블과 스키마/통계/미리보기/검증 정보.

    예외:
        ValueError: preview_limit이 음수인 경우 (#190).
    """
    if preview_limit < 0:
        raise ValueError(f"preview_limit must be >= 0, got {preview_limit}")
    table = normalize_table(
        bronze,
        casts=casts,
        rename=rename,
        derived=derived,
        read_as=read_as,
        null_tokens=null_tokens,
        coalesce=coalesce,
        zfill=zfill,
    )
    validation = validate_table(
        table, required_columns=required_columns, column_dtypes=column_dtypes
    )
    schema = build_schema(table)
    statistics = build_statistics(table)
    preview = build_preview(table, limit=preview_limit)

    return SilverDataset(
        table=table,
        schema=schema,
        statistics=statistics,
        preview=preview,
        validation=validation,
        source_bronze=bronze.source_key,
    )
