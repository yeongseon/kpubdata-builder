"""빌드 미리보기 (#3).

실제 빌드를 전부 돌리기 전에 각 소스의 스키마와 샘플 몇 행만 보여준다. Bronze
fetch 후 Silver를 메모리에서 구성하되 **어떤 산출물 파일도 기록하지 않는다**
(persist 호출 없음). build의 축소판이다.

Quality/Schema 평가는 orchestrator.run_build와 동일한 공통 evaluator
(``quality.evaluate_quality``)를 쓴다 (#486) — Preview와 Build가 같은 데이터/규칙에
대해 다른 판정을 내리는 semantic drift를 만들지 않는다. drift 감지는 하지 않는다
(persist된 이전 run과 비교해야 하는데 preview는 워크스페이스에 아무것도 쓰지 않는다).

Source↔Silver diff와 sampling (#497): 같은 실행 안에서 Bronze 원본 sample과
Silver 변환 sample을 같은 행 인덱스로 골라 셀 단위로 비교한다. Bronze→Silver
경로(normalize/validate)가 행을 filter/reorder하지 않는 현재 구조에서만
``diff_available=true``이며, 그 전제가 깨지면(행 수 불일치 등) 잘못된 index diff
대신 ``diff_available=false``로 fail-closed한다.

sample 행 수는 limit(≤ MAX_PREVIEW_LIMIT, service/app.py)으로 제한되지만 컬럼
수는 어디서도 제한되지 않으므로, wide dataset에서는 셀 단위 diff item 개수가
여전히 무제한일 수 있다. 그래서 diffs 리스트 자체는 MAX_PREVIEW_DIFF_ITEMS로
따로 자르고 그 사실을 ``diff_truncated``로 명시한다 — transform_summary의
집계(changed_cells/changed_rows)는 잘린 뒤에도 정확한 합계를 유지한다.

주요 구성:
    - SampleMode: "first" | "random" sampling 방식
    - PreviewDiffItem: 셀 단위 변경 하나
    - PreviewTransformSummary: 비교 가능한 sample 범위의 변경 요약
    - SourcePreview: 소스별 미리보기 결과
    - PreviewResult: 전체 미리보기 결과
    - preview_build: 미리보기 진입점
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from ..quality import QualityCheckResult, evaluate_quality
from ..spec import BuildSpec, JsonValue, SourceRef
from ..spec.models import QualityPolicy
from ..spec.validator import validate_spec
from ..stages.bronze.build import SourceClient
from ..stages.bronze.resolve import build_bronze_artifact_for_source, source_identity
from ..stages.silver.build import build_silver_dataset
from ..stages.silver.preview import select_preview_rows
from ..tabular import DEFAULT_PREVIEW_LIMIT, PreviewSlice, SchemaInfo, TableStatistics
from ..uploads import UploadRepository

SampleMode = Literal["first", "random"]
_SAMPLE_MODES: tuple[SampleMode, ...] = ("first", "random")

# random sample_mode에서 seed가 생략됐을 때 쓰는 고정 기본값 — "random"이라는
# 이름과 달리 재현 불가능한 wall-clock 기반 시드를 쓰지 않는다 (#497).
DEFAULT_PREVIEW_SEED = 0

# diffs 응답의 방어적 상한 (#497). limit(≤ MAX_PREVIEW_LIMIT, service/app.py)은
# 행 수만 제한하고 컬럼 수는 어디서도 제한하지 않으므로, 셀 단위 diff item 개수는
# rows × changed_columns로 wide dataset에서는 여전히 무제한일 수 있다. 이 상한은
# 실제로 materialize해 응답에 싣는 PreviewDiffItem 개수만 자르고, 상한과 같은
# 값(MAX_PREVIEW_LIMIT)을 재사용해 새 매직넘버를 만들지 않는다 —
# transform_summary.changed_cells/changed_rows는 잘리지 않은 실제 합계를 유지해
# "diff 목록은 잘렸지만 집계는 정확하다"는 계약을 지킨다.
MAX_PREVIEW_DIFF_ITEMS = 1000


@dataclass(frozen=True)
class PreviewDiffItem:
    """Source↔Silver 셀 단위 변경 하나 (#497).

    속성:
        row: source_sample/sample 배열 내 0-based 위치. 전체 데이터셋의 절대 행
            번호가 아니다(둘 다 diff_available=true일 때만 같은 대상을 가리킨다).
        column: 컬럼명.
        before: 변환 전(Bronze raw) 값.
        after: 변환 후(Silver) 값.
        transform: 컬럼에 선언된 캐스팅(schema.casts)이 있을 때만
            ``"cast:{dtype}"`` 형태로 채운다. 그 외에는 추측하지 않고 None.
    """

    row: int
    column: str
    before: JsonValue
    after: JsonValue
    transform: str | None = None


@dataclass(frozen=True)
class PreviewTransformSummary:
    """비교 가능한 sample 범위에서 계산한 변경 요약 (#497).

    전체 dataset 변화량이 아니라 이번 preview 응답에 실린 sample 범위 기준이다.

    속성:
        changed_cells: 값이 달라진 셀 개수.
        changed_rows: 하나 이상의 셀이 달라진 행 개수.
    """

    changed_cells: int
    changed_rows: int


@dataclass(frozen=True)
class SourcePreview:
    """단일 소스의 미리보기 결과.

    속성:
        source_key: 소스 식별자.
        status: "ok" 또는 "failed".
        schema: 추론된 스키마 요약 (실패 시 빈 SchemaInfo).
        preview: 상위 N행 미리보기 (실패 시 빈 PreviewSlice). sample_mode에 따라
            상위 N행 또는 결정적 무작위 N행을 담는다.
        statistics: 전체 테이블 기준 통계 (row_count/null_counts/duplicate_rate,
            #440). 스키마 계약 초안(VAL-4)과 품질 게이트(QG-3)의 근거.
        quality_results: 구조화된 Quality/Schema 평가 결과 (#486). Build와 동일한
            evaluator 결과이며, PASS를 포함한 실제로 평가된 check만 담는다.
        error: 실패 시 오류 메시지.
        source_sample: 변환 전 Bronze 원본 sample (#497). diff_available=false여도
            최선 노력으로 채워진다(실패 시에는 빈 튜플).
        sample_mode: 이 응답에 실제로 적용된 sampling 방식.
        diff_available: source_sample[i]와 preview.rows[i]가 같은 논리 행을
            가리킨다고 보장할 수 있을 때만 true. 행 수 불일치·조회 실패 등에서는
            false이며 diffs/transform_summary는 비운다.
        diffs: diff_available=true일 때만 채워지는 셀 단위 변경 목록. 최대
            MAX_PREVIEW_DIFF_ITEMS개까지만 담긴다(diff_truncated 참고).
        transform_summary: diff_available=true일 때만 채워지는 변경 요약.
            diffs가 잘려도 changed_cells/changed_rows는 잘리지 않은 실제 합계다.
        diff_truncated: diffs가 MAX_PREVIEW_DIFF_ITEMS 상한에 걸려 실제 변경 셀
            전부를 담지 못했으면 true. diff_available=false면 항상 false(애초에
            diff를 시도하지 않았으므로 "잘림"이 아니다).
    """

    source_key: str
    status: str
    schema: SchemaInfo
    preview: PreviewSlice
    statistics: TableStatistics
    quality_results: tuple[QualityCheckResult, ...] = ()
    error: str | None = None
    source_sample: tuple[dict[str, JsonValue], ...] = ()
    sample_mode: SampleMode = "first"
    diff_available: bool = False
    diffs: tuple[PreviewDiffItem, ...] = ()
    transform_summary: PreviewTransformSummary | None = None
    diff_truncated: bool = False


@dataclass(frozen=True)
class PreviewResult:
    """전체 미리보기 결과.

    속성:
        previews: 소스별 미리보기 결과.
    """

    previews: tuple[SourcePreview, ...]


def _fetch_key(source: SourceRef) -> str:
    """Bronze fetch identity 키를 반환한다 (kind별 canonical identity, #498)."""
    provider, dataset = source_identity(source)
    return f"{provider}.{dataset}"


def _output_key(source: SourceRef) -> str:
    """미리보기 결과 표면에 노출할 키 — alias가 있으면 alias."""
    return source.alias if source.alias else _fetch_key(source)


def _select_indices(
    *, total_rows: int, limit: int, sample_mode: SampleMode, seed: int
) -> list[int]:
    """전체 total_rows 중 최대 limit개의 행 인덱스를 오름차순으로 고른다.

    "first": 앞쪽 count개를 그대로 고른다(기존 top-N 동작과 동일).
    "random": seed로 초기화한 전용 ``random.Random`` 인스턴스로 비복원 추출한다.
        전역 random 상태를 건드리지 않고, ``range``를 직접 인덱싱하므로 전체
        데이터셋을 리스트로 복사하거나 셔플하지 않는다(#497 메모리 상한 요건).
        동일 total_rows/limit/seed면 항상 동일한 결과를 반환한다.

    Source/Silver가 이 함수의 결과를 그대로 공유해서 쓰는 것이 diff 정합성의
    전제다 — 둘을 독립적으로 sampling하면 다른 행을 고를 수 있다.
    """
    count = min(limit, total_rows)
    if count <= 0:
        return []
    if sample_mode == "first":
        return list(range(count))
    rng = random.Random(seed)
    return sorted(rng.sample(range(total_rows), count))


def _diff_sample(
    source_rows: Sequence[dict[str, JsonValue]],
    transformed_rows: Sequence[dict[str, JsonValue]],
    *,
    columns: Sequence[str],
    casts: Mapping[str, str] | None,
    max_items: int,
) -> tuple[tuple[PreviewDiffItem, ...], PreviewTransformSummary, bool]:
    """정렬됐다고 이미 보장된 두 행 시퀀스를 셀 단위로 비교한다.

    호출자가 ``source_rows[i]``와 ``transformed_rows[i]``가 같은 논리 행을
    가리킨다고 (diff_available 판정으로) 이미 보장했을 때만 불러야 한다.

    ``columns``는 컬럼 수 제한이 없으므로(#497 sample/diff memory 상한) 실제
    materialize해 반환하는 diff item은 ``max_items``개로 자르되, 잘린 뒤에도
    비교 자체는 계속해 ``changed_cells``/``changed_rows``는 항상 전체 sample
    범위의 정확한 합계를 유지한다 — 집계를 diffs 길이에 종속시키지 않는다.

    반환값: (diffs, transform_summary, truncated). truncated는 실제 변경 셀
    수가 max_items를 넘어 diffs가 전부를 담지 못했으면 true.
    """
    diffs: list[PreviewDiffItem] = []
    changed_rows = 0
    changed_cells = 0
    truncated = False
    for row_index, (before_row, after_row) in enumerate(
        zip(source_rows, transformed_rows, strict=True)
    ):
        row_changed = False
        for column in columns:
            before = before_row.get(column)
            after = after_row.get(column)
            if before == after:
                continue
            row_changed = True
            changed_cells += 1
            if len(diffs) < max_items:
                transform = f"cast:{casts[column]}" if casts and column in casts else None
                diffs.append(
                    PreviewDiffItem(
                        row=row_index,
                        column=column,
                        before=before,
                        after=after,
                        transform=transform,
                    )
                )
            else:
                truncated = True
        if row_changed:
            changed_rows += 1
    summary = PreviewTransformSummary(changed_cells=changed_cells, changed_rows=changed_rows)
    return tuple(diffs), summary, truncated


def _preview_source(
    source: SourceRef,
    *,
    client: SourceClient,
    limit: int,
    quality_policy: QualityPolicy | None,
    sample_mode: SampleMode,
    seed: int,
    upload_repository: UploadRepository | None = None,
    owner_id: str | None = None,
) -> SourcePreview:
    """한 소스를 fetch → Silver(메모리)로 만들어 스키마/샘플/diff/quality 결과를 추출한다.

    ``upload_repository``/``owner_id`` 는 ``kind="file"`` source에서만 쓰인다(#498).
    """
    out_key = _output_key(source)
    try:
        required_columns = source.schema.required if source.schema else ()
        column_dtypes = source.schema.dtypes if source.schema else None
        casts = source.schema.casts if source.schema else None
        # kind(public_api/file/url)에 맞는 resolver로 원시 레코드를 가져온다
        # (#498) — Build와 동일한 resolver를 공유해 preview와 build가 같은
        # source에 대해 항상 같은 데이터를 본다.
        bronze = build_bronze_artifact_for_source(
            source,
            client=client,
            upload_repository=upload_repository,
            owner_id=owner_id,
        )
        silver = build_silver_dataset(
            bronze,
            preview_limit=limit,
            required_columns=required_columns,
            casts=casts,
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

        total_rows = silver.statistics.row_count
        # diff alignment의 진짜 근거는 count가 아니라 현재 Silver 정규화 경로의
        # row-preserving invariant다: normalize_table()은 records_to_dataframe()
        # (레코드 순서 그대로 pl.DataFrame 구성) 다음 cast_columns()(같은 행 수를
        # 유지한 채 값만 캐스팅)만 호출하고, validate_table()은 테이블을 아예
        # 건드리지 않는다 — 어느 단계도 행을 filter/dedup/reorder하지 않는다
        # (test_silver.py::TestRowPreservingInvariant#497이 이 불변조건을 회귀
        # 고정한다). 그 불변조건이 유지되는 한 bronze.raw_records[i]는 항상
        # silver.table의 i번째 행과 같은 논리적 행이므로, 같은 index list를
        # 그대로 양쪽에 재사용해도 안전하다.
        #
        # 아래 count 비교는 그 불변조건이 "이번 실행에서" 실제로 지켜졌는지
        # 확인하는 값싼 runtime guard일 뿐, alignment의 근거 자체가 아니다 —
        # Silver가 향후 dedup/filter를 도입하면 이 코드 경로 자체가 바뀌어야
        # 하고, 이 guard는 count가 바뀐 경우만 잡아낸다(같은 count로 reorder만
        # 하는 가상의 미래 변경은 이 guard로 잡지 못하므로 그런 변경을 추가할
        # 때는 반드시 이 판단을 재검토해야 한다).
        aligned = bronze.record_count == total_rows
        indices = _select_indices(
            total_rows=total_rows, limit=limit, sample_mode=sample_mode, seed=seed
        )

        source_sample = tuple(bronze.raw_records[i] for i in indices) if aligned else ()
        transformed_rows = select_preview_rows(silver.table, indices)
        sample_slice = PreviewSlice(rows=transformed_rows, total_rows=total_rows)

        diff_available = aligned and len(source_sample) == len(transformed_rows)
        if diff_available:
            columns = tuple(column.name for column in silver.schema.columns)
            diffs, transform_summary, diff_truncated = _diff_sample(
                source_sample,
                transformed_rows,
                columns=columns,
                casts=casts,
                max_items=MAX_PREVIEW_DIFF_ITEMS,
            )
        else:
            diffs = ()
            transform_summary = None
            diff_truncated = False

        return SourcePreview(
            source_key=out_key,
            status="ok",
            schema=silver.schema,
            preview=sample_slice,
            statistics=silver.statistics,
            quality_results=quality_results,
            source_sample=source_sample,
            sample_mode=sample_mode,
            diff_available=diff_available,
            diffs=diffs,
            transform_summary=transform_summary,
            diff_truncated=diff_truncated,
        )
    except Exception as exc:  # 미리보기 실패를 결과로 변환
        return SourcePreview(
            source_key=out_key,
            status="failed",
            schema=SchemaInfo(),
            preview=PreviewSlice(rows=(), total_rows=0),
            statistics=TableStatistics(row_count=0, null_counts={}, duplicate_rate=0.0),
            error=str(exc),
            source_sample=(),
            sample_mode=sample_mode,
            diff_available=False,
            diffs=(),
            transform_summary=None,
            diff_truncated=False,
        )


def preview_build(
    spec: BuildSpec,
    *,
    client: SourceClient,
    limit: int = DEFAULT_PREVIEW_LIMIT,
    sample_mode: SampleMode = "first",
    seed: int = DEFAULT_PREVIEW_SEED,
    upload_repository: UploadRepository | None = None,
    owner_id: str | None = None,
) -> PreviewResult:
    """각 소스의 스키마와 샘플 행, Source↔Silver diff를 산출한다 (파일 미기록).

    매개변수:
        spec: 미리볼 빌드 명세.
        client: Bronze fetch에 사용할 kpubdata 호환 클라이언트.
        limit: 미리보기에 포함할 최대 행 수.
        sample_mode: "first"(상위 N행, 기본값) 또는 "random"(결정적 무작위 N행).
        seed: sample_mode="random"일 때 쓰는 시드. 동일 입력+동일 seed는 항상
            동일 sample을 반환한다. sample_mode="first"에서는 쓰이지 않는다.
        upload_repository: ``kind="file"`` source의 업로드 content를 조회할
            저장소 (#498). None이면 file source가 있는 preview는 실패한다.
        owner_id: 업로드 소유권 확인에 쓰이는 stable principal id (#498).

    반환값:
        PreviewResult: 소스별 스키마/샘플/diff.

    예외:
        ValueError: limit이 1보다 작거나 sample_mode가 "first"/"random"이 아닌 경우.
        TypeError: seed가 bool을 포함해 int가 아닌 경우.
        ValidationError: spec이 최소 실행 요건을 충족하지 못한 경우. 유효하지 않은
            spec을 부분 실행하거나 빈 결과로 돌려보내지 않고 빠르게 실패시켜
            서비스 레이어와 동일하게 동작하도록 한다 (#193).
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    if sample_mode not in _SAMPLE_MODES:
        raise ValueError(f"sample_mode must be one of {_SAMPLE_MODES}, got {sample_mode!r}")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError(f"seed must be an int, got {type(seed).__name__}")
    validate_spec(spec)
    previews = tuple(
        _preview_source(
            source,
            client=client,
            limit=limit,
            quality_policy=spec.quality,
            sample_mode=sample_mode,
            seed=seed,
            upload_repository=upload_repository,
            owner_id=owner_id,
        )
        for source in spec.sources
    )
    return PreviewResult(previews=previews)
