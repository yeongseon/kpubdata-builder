"""Bronze/Silver/Gold stage summary/detail 서비스 로직 (#488).

``GET /builds/{run_id}/stages`` 와 ``GET /builds/{run_id}/stages/{stage}`` 가 쓰는
순수 조회 로직을 담는다. run_id 형식 검증·존재 확인·ownership 게이팅은
service/app.py의 dispatch/BuilderService가 먼저 처리하고, 이 모듈의 함수는 그
뒤(신뢰된 run_id)부터 시작한다.

stage 산출물 파일 배치에 대한 지식은 ``stages._stage_reader``에 캡슐화돼 있고,
이 모듈은 manifest(정본)와 그 reader를 조합해 run 단위 응답을 만든다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from ..spec import BuildSpec, SourceRef
from ..stages._stage_reader import (
    BronzeSummary,
    GoldSummary,
    SilverSummary,
    SourceStageSummary,
    StageStatus,
    compute_run_stage_summary,
    read_bronze_summary,
    read_gold_summary,
    read_silver_summary,
)
from ..stages.bronze.resolve import source_identity

Stage = Literal["bronze", "silver", "gold"]
STAGE_NAMES: tuple[Stage, ...] = ("bronze", "silver", "gold")

# Silver sample 응답 limit의 방어적 상한. 실제 반환량은 preview.json에 build 시점
# (DEFAULT_PREVIEW_LIMIT, 기본 5행)만큼만 이미 persist돼 있으므로 이보다 항상
# 작거나 같지만, 터무니없이 큰 limit 요청 자체는 명확한 400으로 거부한다.
MAX_STAGE_PREVIEW_LIMIT = 1000
DEFAULT_STAGE_PREVIEW_LIMIT = 5


def known_source_keys(manifest: dict[str, object]) -> tuple[str, ...]:
    """manifest.inputs에서 이 run에 알려진 source_key(output-facing key) 목록을 얻는다.

    orchestrator가 실제로 시도한 소스만 기록되므로(성공/실패 무관), source query
    파라미터가 이 목록에 있는지 확인하면 filesystem path로 그대로 이어붙이기 전에
    known source인지 검증할 수 있다. inputs 필드가 없는 극히 오래된 legacy
    manifest는 빈 튜플을 반환한다 — 추측하지 않는다.
    """
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list):
        return ()
    return tuple(item for item in inputs if isinstance(item, str))


def failed_source_keys(manifest: dict[str, object]) -> frozenset[str]:
    """manifest.errors(``["{source_key}: {message}", ...]``)에서 실패한 source_key 집합을 얻는다."""
    errors = manifest.get("errors")
    if not isinstance(errors, list):
        return frozenset()
    keys: set[str] = set()
    for entry in errors:
        if not isinstance(entry, str):
            continue
        prefix, sep, _rest = entry.partition(": ")
        if sep:
            keys.add(prefix)
    return frozenset(keys)


def list_run_stages(
    output_root: Path, run_id: str, manifest: dict[str, object]
) -> list[SourceStageSummary]:
    """run에 알려진 모든 source의 Bronze/Silver/Gold 상태를 계산한다."""
    sources = known_source_keys(manifest)
    failed = failed_source_keys(manifest)
    return compute_run_stage_summary(output_root, run_id, sources, failed)


def stage_status_for_source(
    output_root: Path, run_id: str, manifest: dict[str, object], source_key: str
) -> SourceStageSummary | None:
    """단일 source의 stage 상태를 계산한다. known source가 아니면 None."""
    if source_key not in known_source_keys(manifest):
        return None
    failed = failed_source_keys(manifest)
    results = compute_run_stage_summary(output_root, run_id, (source_key,), failed)
    return results[0] if results else None


def stage_status_of(summary: SourceStageSummary, stage: Stage) -> StageStatus:
    if stage == "bronze":
        return summary.bronze
    if stage == "silver":
        return summary.silver
    return summary.gold


def _output_source_key(source: SourceRef) -> str:
    """pipeline.orchestrator._output_source_key와 동일한 규칙을 미러링한다.

    stage 조회는 파이프라인이 실제로 파일을 쓸 때 쓴 것과 같은 output-facing
    key(alias 우선, 아니면 kind별 canonical identity)로 소스를 찾아야 한다.
    file/url kind(#498)는 provider/dataset이 비어 있으므로 orchestrator와
    동일한 source_identity()로 identity를 채운다.
    """
    if source.alias:
        return source.alias
    provider, dataset = source_identity(source)
    return f"{provider}.{dataset}"


def match_source_ref(spec: BuildSpec, source_key: str) -> SourceRef | None:
    """canonical snapshot의 sources 중 output-facing key가 일치하는 것을 찾는다."""
    for source in spec.sources:
        if _output_source_key(source) == source_key:
            return source
    return None


def bronze_detail(output_root: Path, run_id: str, source_key: str) -> BronzeSummary | None:
    return read_bronze_summary(output_root, run_id, source_key)


def silver_detail(
    output_root: Path, run_id: str, source_key: str, *, limit: int
) -> SilverSummary | None:
    return read_silver_summary(output_root, run_id, source_key, sample_limit=limit)


def gold_detail(output_root: Path, run_id: str, source_key: str) -> GoldSummary | None:
    return read_gold_summary(output_root, run_id, source_key)


__all__ = [
    "DEFAULT_STAGE_PREVIEW_LIMIT",
    "MAX_STAGE_PREVIEW_LIMIT",
    "STAGE_NAMES",
    "Stage",
    "bronze_detail",
    "failed_source_keys",
    "gold_detail",
    "known_source_keys",
    "list_run_stages",
    "match_source_ref",
    "silver_detail",
    "stage_status_for_source",
    "stage_status_of",
]
