"""빌드 미리보기 (#3).

실제 빌드를 전부 돌리기 전에 각 소스의 스키마와 샘플 몇 행만 보여준다. Bronze
fetch 후 Silver를 메모리에서 구성하되 **어떤 산출물 파일도 기록하지 않는다**
(persist 호출 없음). build의 축소판이다.

Quality/Schema 평가는 orchestrator.run_build와 동일한 공통 evaluator
(``quality.evaluate_quality``)를 쓴다 (#486) — Preview와 Build가 같은 데이터/규칙에
대해 다른 판정을 내리는 semantic drift를 만들지 않는다. drift 감지는 하지 않는다
(persist된 이전 run과 비교해야 하는데 preview는 워크스페이스에 아무것도 쓰지 않는다).

주요 구성:
    - SourcePreview: 소스별 미리보기 결과
    - PreviewResult: 전체 미리보기 결과
    - preview_build: 미리보기 진입점
"""

from __future__ import annotations

from dataclasses import dataclass

from ..quality import QualityCheckResult, evaluate_quality
from ..spec import BuildSpec, SourceRef
from ..spec.models import QualityPolicy
from ..spec.validator import validate_spec
from ..stages.bronze.build import SourceClient, build_bronze_artifact
from ..stages.silver.build import build_silver_dataset
from ..tabular import DEFAULT_PREVIEW_LIMIT, PreviewSlice, SchemaInfo, TableStatistics


@dataclass(frozen=True)
class SourcePreview:
    """단일 소스의 미리보기 결과.

    속성:
        source_key: 소스 식별자.
        status: "ok" 또는 "failed".
        schema: 추론된 스키마 요약 (실패 시 빈 SchemaInfo).
        preview: 상위 N행 미리보기 (실패 시 빈 PreviewSlice).
        statistics: 전체 테이블 기준 통계 (row_count/null_counts/duplicate_rate,
            #440). 스키마 계약 초안(VAL-4)과 품질 게이트(QG-3)의 근거.
        quality_results: 구조화된 Quality/Schema 평가 결과 (#486). Build와 동일한
            evaluator 결과이며, PASS를 포함한 실제로 평가된 check만 담는다.
        error: 실패 시 오류 메시지.
    """

    source_key: str
    status: str
    schema: SchemaInfo
    preview: PreviewSlice
    statistics: TableStatistics
    quality_results: tuple[QualityCheckResult, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class PreviewResult:
    """전체 미리보기 결과.

    속성:
        previews: 소스별 미리보기 결과.
    """

    previews: tuple[SourcePreview, ...]


def _fetch_key(source: SourceRef) -> str:
    """kpubdata Client.dataset()에 전달할 정규 fetch 키 (provider.dataset)."""
    return f"{source.provider}.{source.dataset}"


def _output_key(source: SourceRef) -> str:
    """미리보기 결과 표면에 노출할 키 — alias가 있으면 alias."""
    return source.alias if source.alias else _fetch_key(source)


def _preview_source(
    source: SourceRef,
    *,
    client: SourceClient,
    limit: int,
    quality_policy: QualityPolicy | None,
) -> SourcePreview:
    """한 소스를 fetch → Silver(메모리)로 만들어 스키마/샘플/quality 결과를 추출한다."""
    # fetch_key는 항상 provider.dataset (kpubdata.Client 계약). 표면 키는 alias 우선.
    fetch_key = _fetch_key(source)
    out_key = _output_key(source)
    try:
        required_columns = source.schema.required if source.schema else ()
        column_dtypes = source.schema.dtypes if source.schema else None
        bronze = build_bronze_artifact(
            client, source_key=fetch_key, fetch_params=dict(source.params)
        )
        silver = build_silver_dataset(
            bronze,
            preview_limit=limit,
            required_columns=required_columns,
            casts=source.schema.casts if source.schema else None,
            column_dtypes=column_dtypes,
        )
        # Build와 동일한 공통 evaluator (#486) — 파일 persist는 하지 않는다.
        quality_results = evaluate_quality(
            silver,
            quality_policy,
            source_key=out_key,
            required_columns=required_columns,
            column_dtypes=column_dtypes,
        )
        return SourcePreview(
            source_key=out_key,
            status="ok",
            schema=silver.schema,
            preview=silver.preview,
            statistics=silver.statistics,
            quality_results=quality_results,
        )
    except Exception as exc:  # 미리보기 실패를 결과로 변환
        return SourcePreview(
            source_key=out_key,
            status="failed",
            schema=SchemaInfo(),
            preview=PreviewSlice(rows=(), total_rows=0),
            statistics=TableStatistics(row_count=0, null_counts={}, duplicate_rate=0.0),
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

    예외:
        ValueError: limit이 1보다 작은 경우.
        ValidationError: spec이 최소 실행 요건을 충족하지 못한 경우. 유효하지 않은
            spec을 부분 실행하거나 빈 결과로 돌려보내지 않고 빠르게 실패시켜
            서비스 레이어와 동일하게 동작하도록 한다 (#193).
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    validate_spec(spec)
    previews = tuple(
        _preview_source(source, client=client, limit=limit, quality_policy=spec.quality)
        for source in spec.sources
    )
    return PreviewResult(previews=previews)
