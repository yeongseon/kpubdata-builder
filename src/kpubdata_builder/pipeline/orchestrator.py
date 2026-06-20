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

from dataclasses import dataclass
from pathlib import Path

from ..errors import DatasetValidationError
from ..manifest import (
    BuildManifest,
    SchemaSummary,
    SourceProvenance,
    build_schema_summary,
    build_source_provenance,
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


def _run_source_pipeline(
    source: SourceRef,
    *,
    client: SourceClient,
    context: BuildContext,
    outputs: list[str],
    row_counts: dict[str, int],
    schema_summaries: dict[str, SchemaSummary],
    provenance: list[SourceProvenance],
) -> SourceBuildOutcome:
    """한 소스를 Bronze → Silver → Gold로 실행하고 산출물을 저장한다."""
    fetch_key = _fetch_source_key(source)
    output_key = _output_source_key(source)
    completed: list[str] = []
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
        provenance.append(
            build_source_provenance(
                provider=source.provider,
                dataset=source.dataset,
                fetched_at=bronze.fetched_at,
                records=bronze.raw_records,
                params=source.params,
            )
        )

        silver = build_silver_dataset(bronze)
        # 검증에 실패한 Silver 데이터셋이 Gold/패키징으로 흘러가지 않도록 소스를
        # 실패 처리한다. 검증은 더 이상 권고용이 아니라 게이트다 (#189).
        if not silver.validation.ok:
            raise DatasetValidationError(list(silver.validation.problems))
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

        gold = build_gold_package(silver, dataset_name=output_key, exports=context.spec.exports)
        gold_paths = persist_gold_package(
            gold, output_root=context.output_root, run_id=context.run_id
        )
        completed.append("gold")
        _record_output_paths(outputs, gold_paths.table_path, gold_paths.package_path)

        card = build_dataset_card(
            title=context.spec.title,
            description=context.spec.description,
            sources=(fetch_key,),
            fields=(
                (column.name, column.dtype, column.nullable) for column in silver.schema.columns
            ),
            sample_rows=silver.preview.rows,
            license=context.spec.metadata.get("license", ""),
            version=context.spec.metadata.get("version", ""),
        )
        card_path = gold_paths.gold_dir / "README.md"
        _ = card_path.write_text(render_dataset_card(card), encoding="utf-8")
        _record_output_paths(outputs, card_path)

        row_counts[output_key] = silver.statistics.row_count
        schema_summaries[output_key] = build_schema_summary(
            (column.name, column.dtype, column.nullable) for column in silver.schema.columns
        )
        return SourceBuildOutcome(
            source_key=output_key, status="ok", stages_completed=tuple(completed)
        )
    except Exception as exc:  # stage 실패를 결과로 변환하여 매니페스트에 기록
        return SourceBuildOutcome(
            source_key=output_key,
            status="failed",
            stages_completed=tuple(completed),
            error=str(exc),
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

    outputs: list[str] = []
    row_counts: dict[str, int] = {}
    schema_summaries: dict[str, SchemaSummary] = {}
    provenance: list[SourceProvenance] = []
    outcomes = tuple(
        _run_source_pipeline(
            source,
            client=client,
            context=context,
            outputs=outputs,
            row_counts=row_counts,
            schema_summaries=schema_summaries,
            provenance=provenance,
        )
        for source in spec.sources
    )

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
    )
    manifest_path = context.output_root / context.run_id / "manifest.json"
    manifest_writer(manifest, manifest_path)

    return BuildResult(
        context=context,
        status=status,
        outcomes=outcomes,
        manifest_path=manifest_path,
    )
