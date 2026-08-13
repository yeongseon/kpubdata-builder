"""Bronze/Silver/Gold stage 산출물을 안전하게 조회하기 위한 공용 reader (#488).

Studio 같은 소비자가 Bronze/Silver/Gold의 실제 파일 배치(``{run}/bronze/{source_key}/
{artifact_id}/raw_records.jsonl`` 등)를 알아야 stage 상태나 미리보기를 얻을 수 있으면
Builder 저장 구조에 결합된다. 이 모듈이 그 지식을 캡슐화해서, 서비스 레이어는
source_key와 stage 이름만으로 상태/요약을 얻을 수 있게 한다.

파일시스템 존재만으로 성공을 추측하지 않는다 — 완전한 sidecar 파일 집합이 있을 때만
"completed"로 판정하고, 디렉터리는 있지만 일부만 있으면 "unavailable"(손상/legacy),
상위 단계가 끝나지 못해 아예 시도되지 않았으면 "not_run", manifest에 실패로 기록된
소스인데 완전한 산출물이 없으면 "failed"로 구분한다.

주요 구성:
    - StageStatus: completed/failed/not_run/unavailable
    - SourceStageSummary: 소스 하나의 3단계 상태 요약
    - compute_run_stage_summary: run의 모든 소스에 대한 stage 상태 계산
    - read_bronze_summary / read_silver_summary / read_gold_summary: 안전한 상세 조회
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..spec import JsonValue
from ._path_safety import ensure_within, validate_path_segment

StageStatus = Literal["completed", "failed", "not_run", "unavailable"]

_SILVER_SIDECAR_FILES = ("schema.json", "stats.json", "preview.json", "validation.json")


def sanitize_source_segment(source_key: str) -> str:
    """Bronze/Silver persist가 쓰는 것과 동일한 source_key 파일명 정리 규칙 (bronze/persist.py,
    silver/persist.py 참조). Gold는 source_key를 그대로 dataset_name으로 쓴다."""
    return source_key.replace("/", "_")


def bronze_source_dir(output_root: Path, run_id: str, source_key: str) -> Path:
    """{output_root}/{run_id}/bronze/{sanitized source_key} 경로. 안전하지 않으면 ValueError."""
    segment = sanitize_source_segment(source_key)
    validate_path_segment(segment, field_name="source_key")
    run_dir = output_root / run_id
    ensure_within(output_root, run_dir, label="run directory")
    stage_dir = run_dir / "bronze" / segment
    ensure_within(output_root, stage_dir, label="bronze source directory")
    return stage_dir


def silver_source_dir(output_root: Path, run_id: str, source_key: str) -> Path:
    """{output_root}/{run_id}/silver/{sanitized source_key} 경로. 안전하지 않으면 ValueError."""
    segment = sanitize_source_segment(source_key)
    validate_path_segment(segment, field_name="source_key")
    run_dir = output_root / run_id
    ensure_within(output_root, run_dir, label="run directory")
    stage_dir = run_dir / "silver" / segment
    ensure_within(output_root, stage_dir, label="silver source directory")
    return stage_dir


def gold_source_dir(output_root: Path, run_id: str, source_key: str) -> Path:
    """{output_root}/{run_id}/gold/{source_key} 경로. gold persist는 slash를 치환하지 않고
    source_key(=dataset_name)를 그대로 쓰므로(gold/persist.py) 여기도 그대로 쓴다.
    안전하지 않으면 ValueError."""
    validate_path_segment(source_key, field_name="source_key")
    run_dir = output_root / run_id
    ensure_within(output_root, run_dir, label="run directory")
    stage_dir = run_dir / "gold" / source_key
    ensure_within(output_root, stage_dir, label="gold source directory")
    return stage_dir


def _read_json(path: Path) -> JsonValue | None:
    """JSON 파일을 안전하게 읽는다. 없거나 손상되면 None (crash 대신 unavailable로 표현)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        return None


def _select_latest_bronze_artifact(candidate_dirs: Sequence[Path]) -> Path | None:
    """동일 source 아래 여러 artifact_id 후보가 있을 때 결정적으로 하나를 선택한다 (#488).

    각 후보의 metadata.json에서 fetched_at을 읽어 가장 최근인 것을 고른다(ISO 8601
    문자열은 사전식 정렬이 시간 순서와 일치한다). 동일 fetched_at이면 artifact_id
    (디렉터리 이름) 내림차순으로 타이브레이크한다. metadata.json을 읽을 수 없는
    후보는 후보군에서 제외한다 — 임의로 하나를 고르지 않는다.
    """
    scored: list[tuple[str, str, Path]] = []
    for candidate in candidate_dirs:
        meta = _read_json(candidate / "metadata.json")
        if not isinstance(meta, dict):
            continue
        if not (candidate / "raw_records.jsonl").is_file():
            continue
        fetched_at = meta.get("fetched_at")
        sort_key = fetched_at if isinstance(fetched_at, str) else ""
        scored.append((sort_key, candidate.name, candidate))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2]


def _bronze_artifact_dir(output_root: Path, run_id: str, source_key: str) -> Path | None:
    try:
        src_dir = bronze_source_dir(output_root, run_id, source_key)
    except ValueError:
        return None
    if not src_dir.is_dir():
        return None
    candidates = [child for child in src_dir.iterdir() if child.is_dir()]
    return _select_latest_bronze_artifact(candidates)


def _silver_complete(output_root: Path, run_id: str, source_key: str) -> tuple[bool, bool]:
    """(완전 여부, 디렉터리 존재 여부)를 반환한다."""
    try:
        d = silver_source_dir(output_root, run_id, source_key)
    except ValueError:
        return False, False
    if not d.is_dir():
        return False, False
    complete = (d / "table.parquet").is_file() and all(
        (d / name).is_file() and _read_json(d / name) is not None for name in _SILVER_SIDECAR_FILES
    )
    return complete, True


def _gold_complete(output_root: Path, run_id: str, source_key: str) -> tuple[bool, bool]:
    """(완전 여부, 디렉터리 존재 여부)를 반환한다."""
    try:
        d = gold_source_dir(output_root, run_id, source_key)
    except ValueError:
        return False, False
    if not d.is_dir():
        return False, False
    package_path = d / "package.json"
    complete = (
        (d / "table.parquet").is_file()
        and package_path.is_file()
        and _read_json(package_path) is not None
    )
    return complete, True


def _stage_status(
    *, complete: bool, dir_exists: bool, upstream_completed: bool, source_failed: bool
) -> StageStatus:
    """단일 stage의 상태를 결정한다.

    우선순위: 완전한 산출물이 있으면 completed. 디렉터리는 있는데 불완전하면
    unavailable(손상/legacy 형식). 상위 단계가 끝나지 못했으면 이 단계는 시도조차
    되지 않았으므로 not_run. manifest가 이 소스를 실패로 기록했으면 failed.
    그 외(알 수 없는 상태)는 not_run으로 보수적으로 표시한다.
    """
    if complete:
        return "completed"
    if dir_exists:
        return "unavailable"
    if not upstream_completed:
        return "not_run"
    if source_failed:
        return "failed"
    return "not_run"


@dataclass(frozen=True)
class SourceStageSummary:
    """소스 하나의 Bronze/Silver/Gold 상태 요약."""

    source_key: str
    bronze: StageStatus
    silver: StageStatus
    gold: StageStatus


def compute_run_stage_summary(
    output_root: Path,
    run_id: str,
    source_keys: Sequence[str],
    failed_source_keys: frozenset[str],
) -> list[SourceStageSummary]:
    """run에 알려진 각 소스에 대해 Bronze/Silver/Gold 상태를 계산한다."""
    results: list[SourceStageSummary] = []
    for source_key in source_keys:
        source_failed = source_key in failed_source_keys

        bronze_artifact = _bronze_artifact_dir(output_root, run_id, source_key)
        try:
            bronze_dir_exists = bronze_source_dir(output_root, run_id, source_key).is_dir()
        except ValueError:
            bronze_dir_exists = False
        bronze_status = _stage_status(
            complete=bronze_artifact is not None,
            dir_exists=bronze_dir_exists,
            upstream_completed=True,
            source_failed=source_failed,
        )

        silver_complete, silver_dir_exists = _silver_complete(output_root, run_id, source_key)
        silver_status = _stage_status(
            complete=silver_complete,
            dir_exists=silver_dir_exists,
            upstream_completed=(bronze_status == "completed"),
            source_failed=source_failed,
        )

        gold_complete, gold_dir_exists = _gold_complete(output_root, run_id, source_key)
        gold_status = _stage_status(
            complete=gold_complete,
            dir_exists=gold_dir_exists,
            upstream_completed=(silver_status == "completed"),
            source_failed=source_failed,
        )

        results.append(
            SourceStageSummary(
                source_key=source_key, bronze=bronze_status, silver=silver_status, gold=gold_status
            )
        )
    return results


@dataclass(frozen=True)
class BronzeSummary:
    """안전하게 노출 가능한 Bronze 요약. fetch_params/provenance 원문은 담지 않는다."""

    fetched_at: str | None
    record_count: int | None


def read_bronze_summary(output_root: Path, run_id: str, source_key: str) -> BronzeSummary | None:
    """선택된 Bronze artifact의 안전한 summary만 읽는다.

    fetch_params, provenance.fetch_params(secret 가능성), artifact_paths(내부 파일
    배치)는 절대 반환하지 않는다.
    """
    artifact_dir = _bronze_artifact_dir(output_root, run_id, source_key)
    if artifact_dir is None:
        return None
    meta = _read_json(artifact_dir / "metadata.json")
    if not isinstance(meta, dict):
        return None
    fetched_at = meta.get("fetched_at")
    record_count = meta.get("record_count")
    return BronzeSummary(
        fetched_at=fetched_at if isinstance(fetched_at, str) else None,
        record_count=record_count if isinstance(record_count, int) else None,
    )


@dataclass(frozen=True)
class SilverSummary:
    """안전하게 노출 가능한 Silver 요약. sample은 호출자가 넘긴 상한으로 이미 잘려 있다."""

    row_count: int | None
    schema: list[JsonValue]
    statistics: JsonValue
    validation: JsonValue
    sample: list[JsonValue]
    sample_total_available: int


def read_silver_summary(
    output_root: Path, run_id: str, source_key: str, *, sample_limit: int
) -> SilverSummary | None:
    """schema.json/stats.json/validation.json/preview.json에서 안전한 요약을 읽는다.

    parquet 전체는 읽지 않는다 — sample은 항상 preview.json에 이미 persist 시점에
    저장된 상한 내(build_silver_dataset의 preview_limit)에서만 나온다.
    """
    try:
        d = silver_source_dir(output_root, run_id, source_key)
    except ValueError:
        return None
    if not d.is_dir():
        return None
    schema = _read_json(d / "schema.json")
    stats = _read_json(d / "stats.json")
    validation = _read_json(d / "validation.json")
    preview = _read_json(d / "preview.json")
    if schema is None or stats is None or validation is None or preview is None:
        return None

    columns = schema.get("columns") if isinstance(schema, dict) else None
    row_count = stats.get("row_count") if isinstance(stats, dict) else None
    rows = preview.get("rows") if isinstance(preview, dict) else None
    all_rows = rows if isinstance(rows, list) else []
    capped_limit = max(sample_limit, 0)
    sample = all_rows[:capped_limit]

    return SilverSummary(
        row_count=row_count if isinstance(row_count, int) else None,
        schema=columns if isinstance(columns, list) else [],
        statistics=stats,
        validation=validation,
        sample=sample,
        sample_total_available=len(all_rows),
    )


@dataclass(frozen=True)
class GoldSummary:
    """안전하게 노출 가능한 Gold 요약. export options/output_path/credential은 담지 않는다."""

    row_count: int | None
    columns: list[str]
    splits: dict[str, int] | None
    export_kinds: list[str]


def read_gold_summary(output_root: Path, run_id: str, source_key: str) -> GoldSummary | None:
    """package.json에서 안전한 요약만 읽는다.

    export_plan.targets[].options(credential 가능성)와 output_path는 반환하지
    않는다 — kind만 노출한다. Gold sample sidecar는 아직 없으므로 만들어내지
    않는다(호출자가 sample_available=false로 표현).
    """
    try:
        d = gold_source_dir(output_root, run_id, source_key)
    except ValueError:
        return None
    if not d.is_dir():
        return None
    package = _read_json(d / "package.json")
    if not isinstance(package, dict):
        return None

    row_count = package.get("row_count")
    columns = package.get("columns")
    splits_raw = package.get("splits")
    splits: dict[str, int] | None = None
    if isinstance(splits_raw, dict):
        splits = {str(name): count for name, count in splits_raw.items() if isinstance(count, int)}

    kinds: list[str] = []
    export_plan = package.get("export_plan")
    if isinstance(export_plan, dict):
        targets = export_plan.get("targets")
        if isinstance(targets, list):
            kinds = [
                kind
                for target in targets
                if isinstance(target, dict) and isinstance(kind := target.get("kind"), str)
            ]

    return GoldSummary(
        row_count=row_count if isinstance(row_count, int) else None,
        columns=[c for c in columns if isinstance(c, str)] if isinstance(columns, list) else [],
        splits=splits,
        export_kinds=kinds,
    )


__all__ = [
    "BronzeSummary",
    "GoldSummary",
    "SilverSummary",
    "SourceStageSummary",
    "StageStatus",
    "bronze_source_dir",
    "compute_run_stage_summary",
    "gold_source_dir",
    "read_bronze_summary",
    "read_gold_summary",
    "read_silver_summary",
    "sanitize_source_segment",
    "silver_source_dir",
]
