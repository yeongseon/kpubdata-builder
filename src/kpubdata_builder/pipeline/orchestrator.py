"""Medallion 파이프라인 오케스트레이터 (#48).

BuildSpec의 각 소스를 Bronze → Silver → Gold 순서로 실행하고, 각 단계 산출물을
실행 워크스페이스에 저장한 뒤 빌드 매니페스트를 기록한다.

부분 성공 정책(BUILD_STATE.md): 소스 중 하나라도 실패하면 전체 상태는 failed로
기록하되, 성공한 소스의 산출물과 실패 정보를 매니페스트에 함께 남긴다.

Export 단계 연결은 stage-aware exporter 도입(#28/v0.2) 시점으로 연기한다.

주요 구성:
    - SourceBuildOutcome: 소스별 실행 결과
    - BuildResult: 전체 실행 결과
    - run_build: 파이프라인 진입점
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from ..errors import DatasetValidationError, ValidationError
from ..manifest import (
    BuildManifest,
    SchemaSummary,
    SourceProvenance,
    build_schema_summary,
    build_source_provenance,
    capture_build_environment,
    compute_inputs_fingerprint,
    manifest_writer,
)
from ..spec import BuildSpec, SourceRef
from ..spec.validator import validate_spec
from ..stages.bronze.build import SourceClient, build_bronze_artifact
from ..stages.bronze.models import BronzeArtifact, utc_now
from ..stages.bronze.persist import persist_bronze_artifact
from ..stages.gold.build import build_gold_package
from ..stages.gold.card import build_dataset_card, render_dataset_card
from ..stages.gold.persist import persist_gold_package
from ..stages.silver.build import build_silver_dataset
from ..stages.silver.persist import persist_silver_dataset
from .context import BuildContext

logger = logging.getLogger(__name__)

# 소스를 동시에 실행할 최대 스레드 수. 소스별 fetch/stage는 대부분 네트워크 I/O로
# 대기하므로 순차 실행 시 총 소요 시간이 소스 수에 비례해 늘어난다. 소스 수만큼
# 무제한으로 스레드를 만들지 않도록 상한을 둔다 (#247).
_MAX_PARALLEL_SOURCES = 4


@dataclass(frozen=True)
class SourceBuildOutcome:
    """단일 소스에 대한 파이프라인 실행 결과.

    속성:
        source_key: 소스 식별자.
        status: "ok" 또는 "failed".
        stages_completed: 성공적으로 끝난 단계 이름 순서 (bronze/silver/gold).
        error: 실패 시 오류 메시지.
    """

    source_key: str
    status: str
    stages_completed: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class BuildResult:
    """전체 빌드 실행 결과.

    속성:
        context: 실행 컨텍스트.
        status: 전체 상태 ("ok" 또는 "failed").
        outcomes: 소스별 실행 결과.
        manifest_path: 기록된 빌드 매니페스트 경로.
    """

    context: BuildContext
    status: str
    outcomes: tuple[SourceBuildOutcome, ...]
    manifest_path: Path


def _fetch_source_key(source: SourceRef) -> str:
    """Bronze fetch에 사용할 실제 provider.dataset 키를 반환한다."""
    return f"{source.provider}.{source.dataset}"


def _output_source_key(source: SourceRef) -> str:
    """워크스페이스/결과 기록에 사용할 사용자 노출 키를 반환한다."""
    return source.alias if source.alias else _fetch_source_key(source)


def _retag_bronze_artifact(artifact: BronzeArtifact, *, output_key: str) -> BronzeArtifact:
    """fetch provenance는 유지하고 산출물 경로용 source_key만 교체한다."""
    return BronzeArtifact(
        source_key=output_key,
        raw_records=artifact.raw_records,
        fetch_params=artifact.fetch_params,
        fetched_at=artifact.fetched_at,
        provenance=artifact.provenance,
    )


def _record_output_paths(outputs: list[str], *paths: Path) -> None:
    """생성된 산출물 경로를 manifest outputs에 모두 기록한다."""
    outputs.extend(str(path) for path in paths)


@dataclass(frozen=True)
class _SourcePipelineResult:
    """단일 소스 파이프라인 실행의 로컬 결과.

    여러 소스를 스레드 풀로 동시에 실행할 때(#247), _run_source_pipeline이 공유
    가변 상태(outputs/row_counts/schema_summaries/provenance)를 직접 건드리지
    않고 자신의 결과만 반환하게 한다. 병합은 run_build에서 모든 스레드가 끝난
    뒤 단일 스레드로 수행한다.
    """

    outcome: SourceBuildOutcome
    output_paths: tuple[str, ...] = ()
    row_count: int | None = None
    schema_summary: SchemaSummary | None = None
    provenance_entry: SourceProvenance | None = None


def _run_source_pipeline(
    source: SourceRef,
    *,
    client: SourceClient,
    context: BuildContext,
) -> _SourcePipelineResult:
    """한 소스를 Bronze → Silver → Gold로 실행하고 산출물을 저장한다.

    공유 가변 컨테이너를 인자로 받는 대신 결과를 로컬로 모아 반환하므로,
    여러 소스에 대해 동시에(스레드 풀에서) 안전하게 호출할 수 있다 (#247).
    """
    fetch_key = _fetch_source_key(source)
    output_key = _output_source_key(source)
    completed: list[str] = []
    outputs: list[str] = []
    provenance_entry: SourceProvenance | None = None
    try:
        bronze = build_bronze_artifact(
            client, source_key=fetch_key, fetch_params=dict(source.params)
        )
        bronze = _retag_bronze_artifact(bronze, output_key=output_key)
        bronze_paths = persist_bronze_artifact(
            bronze, output_root=context.output_root, run_id=context.run_id
        )
        completed.append("bronze")
        _record_output_paths(outputs, bronze_paths.records_path, bronze_paths.metadata_path)
        # bronze 성공 직후 확정한다: 이후 단계가 실패해도(부분 실패) provenance는 남는다
        # (병렬화 이전 shared list에 즉시 append하던 것과 동일한 동작을 유지) (#247).
        provenance_entry = build_source_provenance(
            provider=source.provider,
            dataset=source.dataset,
            fetched_at=bronze.fetched_at,
            records=bronze.raw_records,
            params=source.params,
        )

        silver = build_silver_dataset(bronze)
        # 검증에 실패한 Silver 데이터셋이 Gold/패키징으로 흘러가지 않도록 소스를
        # 실패 처리한다. 검증은 더 이상 권고용이 아니라 게이트다 (#189).
        if not silver.validation.ok:
            # ValidationProblem 객체를 DatasetValidationError가 기대하는 문자열 목록으로 변환 (#261)
            problem_messages = [problem.message for problem in silver.validation.problems]
            raise DatasetValidationError(problem_messages)
        silver_paths = persist_silver_dataset(
            silver, output_root=context.output_root, run_id=context.run_id
        )
        completed.append("silver")
        _record_output_paths(
            outputs,
            silver_paths.table_path,
            silver_paths.schema_path,
            silver_paths.stats_path,
            silver_paths.preview_path,
            silver_paths.validation_path,
        )

        gold = build_gold_package(
            silver,
            dataset_name=output_key,
            exports=context.spec.exports,
            splits_spec=context.spec.splits,
        )
        gold_paths = persist_gold_package(
            gold, output_root=context.output_root, run_id=context.run_id
        )
        completed.append("gold")
        _record_output_paths(
            outputs,
            gold_paths.table_path,
            gold_paths.package_path,
            *gold_paths.splits_paths.values(),
        )

        card = build_dataset_card(
            title=context.spec.title,
            description=context.spec.description,
            sources=(output_key,),
            fields=(
                (column.name, column.dtype, column.nullable) for column in silver.schema.columns
            ),
            sample_rows=silver.preview.rows,
            license=str(context.spec.metadata.get("license", "")),
            version=str(context.spec.metadata.get("version", "")),
        )
        card_path = gold_paths.gold_dir / "README.md"
        _ = card_path.write_text(render_dataset_card(card), encoding="utf-8")
        _record_output_paths(outputs, card_path)

        schema_summary = build_schema_summary(
            (column.name, column.dtype, column.nullable) for column in silver.schema.columns
        )
        return _SourcePipelineResult(
            outcome=SourceBuildOutcome(
                source_key=output_key, status="ok", stages_completed=tuple(completed)
            ),
            output_paths=tuple(outputs),
            row_count=silver.statistics.row_count,
            schema_summary=schema_summary,
            provenance_entry=provenance_entry,
        )
    except Exception as exc:  # stage 실패를 결과로 변환하여 매니페스트에 기록
        # 검증 오류(ValidationError, DatasetValidationError)는 파일시스템 경로를
        # 포함하지 않으므로 메시지를 그대로 전달한다.
        # ExportError/ManifestError 등 다른 BuildError 하위 예외는 목적지 경로
        # 같은 내부 정보를 메시지에 포함할 수 있으므로, 상세 내용은 서버 경고로
        # 기록하고 클라이언트에는 일반 메시지만 반환한다 (#225).
        if isinstance(exc, (ValidationError, DatasetValidationError)):
            error_msg = str(exc)
        else:
            logger.error(
                "source pipeline failed for %r: %s",
                output_key,
                exc,
                exc_info=exc,
            )
            error_msg = f"pipeline failed for source {output_key!r}"
        return _SourcePipelineResult(
            outcome=SourceBuildOutcome(
                source_key=output_key,
                status="failed",
                stages_completed=tuple(completed),
                error=error_msg,
            ),
            output_paths=tuple(outputs),
            provenance_entry=provenance_entry,
        )


def run_build(
    spec: BuildSpec,
    *,
    client: SourceClient,
    output_root: Path,
    run_id: str | None = None,
) -> BuildResult:
    """BuildSpec을 Medallion 파이프라인으로 실행한다.

    매개변수:
        spec: 실행할 빌드 명세.
        client: Bronze fetch에 사용할 kpubdata 호환 클라이언트.
        output_root: 실행 워크스페이스 루트.
        run_id: 실행 식별자. 생략 시 타임스탬프 기반으로 생성.

    반환값:
        BuildResult: 전체 상태, 소스별 결과, 매니페스트 경로.

    예외:
        ValidationError: spec이 최소 실행 요건을 만족하지 못한 경우.
        ValueError: run_id에 안전하지 않은 문자가 포함된 경우.
    """
    # 진입점에서 spec을 먼저 검증한다(fail-fast). 검증을 호출자에게만 맡기면 잘못된
    # spec이 단계 깊숙이 들어가 cryptic 에러로 터지므로, 단계 진입 전에 막는다 (#212).
    validate_spec(spec)
    context = BuildContext.create(spec, output_root=output_root, run_id=run_id)

    def _worker(source: SourceRef) -> _SourcePipelineResult:
        return _run_source_pipeline(source, client=client, context=context)

    # 소스별 fetch/stage는 대부분 네트워크 I/O 대기이므로 스레드 풀로 동시에 실행해
    # 총 소요 시간을 줄인다 (#247). executor.map은 완료 순서가 아니라 spec.sources
    # 순서로 결과를 반환하므로 이후 병합 결과(매니페스트)가 결정적으로 유지된다.
    max_workers = min(len(spec.sources), _MAX_PARALLEL_SOURCES)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(_worker, spec.sources))

    outcomes = tuple(result.outcome for result in results)

    # 모든 소스 실행이 끝난 뒤 단일 스레드에서 병합한다 — 스레드 간 공유 가변
    # 상태가 없으므로 이 단계는 병렬화 이전과 동일하게 안전하다 (#247).
    outputs: list[str] = []
    row_counts: dict[str, int] = {}
    schema_summaries: dict[str, SchemaSummary] = {}
    provenance: list[SourceProvenance] = []
    for result in results:
        outputs.extend(result.output_paths)
        if result.row_count is not None:
            row_counts[result.outcome.source_key] = result.row_count
        if result.schema_summary is not None:
            schema_summaries[result.outcome.source_key] = result.schema_summary
        if result.provenance_entry is not None:
            provenance.append(result.provenance_entry)

    errors = tuple(
        f"{outcome.source_key}: {outcome.error}"
        for outcome in outcomes
        if outcome.status == "failed" and outcome.error is not None
    )
    status = "ok" if not errors else "failed"

    manifest = BuildManifest(
        build_id=context.run_id,
        started_at=context.started_at,
        finished_at=utc_now(),
        inputs=tuple(_output_source_key(source) for source in spec.sources),
        outputs=tuple(outputs),
        errors=errors,
        row_counts=row_counts,
        schema_summaries=schema_summaries,
        provenance=tuple(provenance),
        build_environment=capture_build_environment(),
        inputs_fingerprint=compute_inputs_fingerprint(provenance),
    )
    manifest_path = context.output_root / context.run_id / "manifest.json"
    manifest_writer(manifest, manifest_path)

    return BuildResult(
        context=context,
        status=status,
        outcomes=outcomes,
        manifest_path=manifest_path,
    )
