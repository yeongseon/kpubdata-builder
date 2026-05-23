"""빌드 미리보기 (#3).

실제 빌드를 전부 돌리기 전에 각 소스의 스키마와 샘플 몇 행만 보여준다. Bronze
fetch 후 Silver를 메모리에서 구성하되 **어떤 산출물 파일도 기록하지 않는다**
(persist 호출 없음). build의 축소판이다.

주요 구성:
    - SourcePreview: 소스별 미리보기 결과
    - PreviewResult: 전체 미리보기 결과
    - preview_build: 미리보기 진입점
"""

from __future__ import annotations

from dataclasses import dataclass

from ..spec import BuildSpec, SourceRef
from ..stages.bronze.build import SourceClient, build_bronze_artifact
from ..stages.silver.build import build_silver_dataset
from ..tabular import DEFAULT_PREVIEW_LIMIT, PreviewSlice, SchemaInfo


@dataclass(frozen=True)
class SourcePreview:
    """단일 소스의 미리보기 결과.

    속성:
        source_key: 소스 식별자.
        status: "ok" 또는 "failed".
        schema: 추론된 스키마 요약 (실패 시 빈 SchemaInfo).
        preview: 상위 N행 미리보기 (실패 시 빈 PreviewSlice).
        error: 실패 시 오류 메시지.
    """

    source_key: str
    status: str
    schema: SchemaInfo
    preview: PreviewSlice
    error: str | None = None


@dataclass(frozen=True)
class PreviewResult:
    """전체 미리보기 결과.

    속성:
        previews: 소스별 미리보기 결과.
    """

    previews: tuple[SourcePreview, ...]


def _source_key(source: SourceRef) -> str:
    """소스 식별자를 alias 우선으로 결정한다."""
    return source.alias if source.alias else f"{source.provider}.{source.dataset}"


def _preview_source(
    source: SourceRef,
    *,
    client: SourceClient,
    limit: int,
) -> SourcePreview:
    """한 소스를 fetch → Silver(메모리)로 만들어 스키마/샘플만 추출한다."""
    key = _source_key(source)
    try:
        bronze = build_bronze_artifact(client, source_key=key, fetch_params=dict(source.params))
        silver = build_silver_dataset(bronze, preview_limit=limit)
        return SourcePreview(
            source_key=key,
            status="ok",
            schema=silver.schema,
            preview=silver.preview,
        )
    except Exception as exc:  # 미리보기 실패를 결과로 변환
        return SourcePreview(
            source_key=key,
            status="failed",
            schema=SchemaInfo(),
            preview=PreviewSlice(rows=(), total_rows=0),
            error=str(exc),
        )


def preview_build(
    spec: BuildSpec,
    *,
    client: SourceClient,
    limit: int = DEFAULT_PREVIEW_LIMIT,
) -> PreviewResult:
    """각 소스의 스키마와 샘플 행을 산출한다 (파일 미기록).

    매개변수:
        spec: 미리볼 빌드 명세.
        client: Bronze fetch에 사용할 kpubdata 호환 클라이언트.
        limit: 미리보기에 포함할 최대 행 수.

    반환값:
        PreviewResult: 소스별 스키마/샘플.
    """
    previews = tuple(_preview_source(source, client=client, limit=limit) for source in spec.sources)
    return PreviewResult(previews=previews)
