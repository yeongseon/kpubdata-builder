"""Silver 미리보기 (#46).

정규화된 테이블의 상위 N행 미리보기 슬라이스를 생성한다. tabular 엔진(#49)에
위임한다.

주요 함수:
    - build_preview: pl.DataFrame → PreviewSlice
    - select_preview_rows: pl.DataFrame → 지정 행 인덱스의 레코드 (#497)
"""

from __future__ import annotations

from collections.abc import Sequence

import polars as pl

from ...spec import JsonValue
from ...tabular import DEFAULT_PREVIEW_LIMIT, PreviewSlice, generate_preview
from ...tabular.convert import dataframe_to_records


def build_preview(table: pl.DataFrame, *, limit: int = DEFAULT_PREVIEW_LIMIT) -> PreviewSlice:
    """상위 N행 미리보기 슬라이스를 생성한다."""
    return generate_preview(table, limit=limit)


def select_preview_rows(
    table: pl.DataFrame, indices: Sequence[int]
) -> tuple[dict[str, JsonValue], ...]:
    """지정한 행 인덱스의 레코드를 추출한다 (#497).

    sample_mode="random"처럼 상위 N행이 아니라 임의 인덱스의 sample이 필요할 때
    쓴다. indices의 순서를 그대로 보존하며, polars 타입은 이 함수 밖으로 새지
    않는다(pipeline 계층은 plain dict만 받는다).
    """
    if not indices:
        return ()
    return tuple(dataframe_to_records(table[list(indices)]))
