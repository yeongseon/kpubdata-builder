"""Silver 미리보기 (#46).

정규화된 테이블의 상위 N행 미리보기 슬라이스를 생성한다. tabular 엔진(#49)에
위임한다.

주요 함수:
    - build_preview: pl.DataFrame → PreviewSlice
"""

from __future__ import annotations

import polars as pl

from ...tabular import DEFAULT_PREVIEW_LIMIT, PreviewSlice, generate_preview


def build_preview(table: pl.DataFrame, *, limit: int = DEFAULT_PREVIEW_LIMIT) -> PreviewSlice:
    """상위 N행 미리보기 슬라이스를 생성한다."""
    return generate_preview(table, limit=limit)
