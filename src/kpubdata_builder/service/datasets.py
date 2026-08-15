"""Built Dataset 조회 로직 (#488).

``BuildSpec.dataset_id``를 built dataset identity로 사용해, 같은 dataset_id를
공유하는 여러 run을 하나의 dataset으로 묶는다.

정본은 BuildSpec snapshot(#487)과 manifest.json이다. BuildIndex는 dataset→run
조회 성능을 위한 파생 검색 index일 뿐이며, 인덱스가 비어 있거나 손상돼도(또는
아예 없어도) 이 모듈은 파일시스템에서 직접 정본을 다시 읽어 폴백한다 — ADR 0003이
``/builds``에 적용한 것과 같은 원칙이다. 최종적으로 응답에 실리는 dataset_id는
항상 latest run의 snapshot을 다시 읽어 재검증한 값이다(``build_dataset_summary``) —
인덱스의 캐시된 dataset_id는 후보를 좁히는 용도로만 쓰이고, 손상되거나 오래됐어도
정본을 바꾸지 않는다.

legacy run(#487 이전에 만들어져 buildspec.yaml snapshot이 없는 run)의 dataset_id는
추측하지 않는다 — dataset grouping에서 조용히 제외된다. ``GET /builds``에는 계속
나타난다.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..spec import BuildSpec, JsonValue, parse_spec
from ..spec.serializer import BUILDSPEC_SNAPSHOT_FILENAME
from ..stages._path_safety import ensure_within
from ..store import BuildEntry, BuildIndex
from .auth import Principal
from .ownership import ownership_allows
from .stages import list_run_stages


@dataclass(frozen=True)
class RunRecord:
    """dataset grouping에 필요한 run 단위 경량 요약(sidecar 미포함)."""

    run_id: str
    dataset_id: str
    status: str
    started_at: str | None
    finished_at: str | None
    spec_digest: str | None
    created_by: str | None


def sort_key(record: RunRecord) -> tuple[bool, str, str]:
    """latest 판정 정렬 키 (#488 semantics C).

    (finished_at 존재 여부, finished_at, run_id) 오름차순 비교 — finished_at이
    있는 run이 없는 run보다 항상 최신으로 취급되고, 동일 finished_at은 run_id
    문자열 내림차순으로 결정적으로 타이브레이크한다(정렬 키 자체는 오름차순이므로
    "더 최신"은 이 키가 더 큰 쪽).
    """
    return (record.finished_at is not None, record.finished_at or "", record.run_id)


def is_more_recent(candidate: RunRecord, current: RunRecord) -> bool:
    """candidate가 current보다 최신 run으로 판정되는지."""
    return sort_key(candidate) > sort_key(current)


def pick_latest(records: Sequence[RunRecord]) -> RunRecord:
    """records 중 latest run을 결정적으로 선택한다. records는 비어있지 않아야 한다."""
    latest = records[0]
    for candidate in records[1:]:
        if is_more_recent(candidate, latest):
            latest = candidate
    return latest


def group_latest_by_dataset(records: Sequence[RunRecord]) -> dict[str, RunRecord]:
    """dataset_id별로 latest run을 결정적으로 선택한다."""
    latest: dict[str, RunRecord] = {}
    for record in records:
        current = latest.get(record.dataset_id)
        if current is None or is_more_recent(record, current):
            latest[record.dataset_id] = record
    return latest


def filter_ownership(
    records: Sequence[RunRecord], principal: Principal | None, *, enforce: bool
) -> list[RunRecord]:
    """list_builds의 _apply_ownership과 동일한 정책으로 접근 가능한 run만 남긴다.

    판정은 ``service.ownership.ownership_allows`` 공용 predicate를 쓴다
    (#504 review) — ``query.resolver``/``app._check_ownership``과 같은
    semantics를 공유한다. ENFORCE_OWNERSHIP + oidc principal일 때만
    필터링한다. dev/service principal과 principal=None은 통과(관리자 권한 +
    하위 호환). 동일 dataset_id라도 타 사용자의 run은 grouping/latest 선정에서
    완전히 제외된다(#488 semantics D) — 여기서 걸러진 뒤에야 grouping/latest
    선택이 일어나므로, 다른 사용자의 run이 latest로 뽑히거나 metadata에
    섞이는 일이 없다.
    """
    if not (enforce and principal is not None and principal.kind == "oidc"):
        return list(records)
    return [
        r
        for r in records
        if ownership_allows(created_by=r.created_by, principal=principal, enforce=enforce)
    ]


def read_snapshot_dataset_id(output_root: Path, run_id: str) -> str | None:
    """run의 canonical BuildSpec snapshot에서 dataset_id만 읽는다.

    snapshot이 없거나 읽거나 파싱할 수 없으면 None을 반환한다 — legacy run의
    dataset_id를 추측하지 않는다(#488 semantics B).
    """
    doc = _read_snapshot_yaml(output_root, run_id)
    if doc is None:
        return None
    dataset_id = doc.get("dataset_id")
    return dataset_id if isinstance(dataset_id, str) and dataset_id else None


def read_snapshot_spec(output_root: Path, run_id: str) -> BuildSpec | None:
    """run의 canonical BuildSpec snapshot 전체를 파싱한다. 없거나 실패하면 None."""
    doc = _read_snapshot_yaml(output_root, run_id)
    if doc is None:
        return None
    try:
        return parse_spec(doc)
    except Exception:
        # canonical snapshot이 현재 파서 기대와 어긋나는 극단적 상황을 방어한다
        # (예: 파서 스키마가 바뀐 뒤 예전 snapshot을 읽는 경우). 추측해서 복원하지
        # 않고 조회 불가로 취급한다.
        return None


def _read_snapshot_yaml(output_root: Path, run_id: str) -> dict[str, object] | None:
    run_dir = output_root / run_id
    snapshot_path = run_dir / BUILDSPEC_SNAPSHOT_FILENAME
    try:
        ensure_within(output_root, snapshot_path, label="BuildSpec snapshot")
    except ValueError:
        return None
    if not snapshot_path.is_file():
        return None
    try:
        raw = snapshot_path.read_text(encoding="utf-8")
        doc = yaml.safe_load(raw)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    return doc if isinstance(doc, dict) else None


def read_manifest(output_root: Path, run_id: str) -> dict[str, object] | None:
    """manifest.json을 안전하게 읽는다. run_dir 밖이거나 없거나 손상되면 None."""
    manifest_path = output_root / run_id / "manifest.json"
    try:
        ensure_within(output_root, manifest_path, label="manifest file")
    except ValueError:
        return None
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _entry_to_record(entry: BuildEntry) -> RunRecord | None:
    if entry.dataset_id is None:
        return None
    return RunRecord(
        run_id=entry.run_id,
        dataset_id=entry.dataset_id,
        status=entry.status,
        started_at=entry.started_at,
        finished_at=entry.finished_at,
        spec_digest=entry.spec_digest,
        created_by=entry.created_by,
    )


def collect_run_records_from_index(build_index: BuildIndex) -> list[RunRecord] | None:
    """BuildIndex에서 dataset_id가 있는 모든 run을 가져온다.

    인덱스가 비어 있으면(ADR 0003과 동일하게 "아직 채워지지 않았을 수 있다"로
    해석) None을 반환해 호출자가 파일시스템 폴백으로 넘어가게 한다. 인덱스 조회가
    예외를 던져도 마찬가지로 폴백한다 — 인덱스는 파생물이므로 조회 실패가 조회
    자체의 실패가 되어서는 안 된다.
    """
    try:
        entries = build_index.list_builds(limit=None)
    except Exception:
        return None
    if not entries:
        return None
    return [record for entry in entries if (record := _entry_to_record(entry)) is not None]


def collect_run_records_from_index_for_dataset(
    build_index: BuildIndex, dataset_id: str
) -> list[RunRecord] | None:
    """BuildIndex에서 특정 dataset_id의 run만 가져온다.

    이 dataset_id에 대한 결과가 비어 있으면, 인덱스 자체가 아직 채워지지 않았을
    가능성과(ADR 0003) 이 dataset_id가 정말로 없는 경우를 구분해야 한다 — 후자만
    신뢰할 수 있는 "없음"이다. 인덱스에 다른 run이라도 하나 있으면 채워져 있다고
    보고 빈 결과를 그대로 신뢰하고, 인덱스 자체가 완전히 비어 있으면 아직 채워지지
    않았을 수 있으므로 None을 반환해 호출자가 파일시스템 폴백으로 넘어가게 한다.
    조회 자체가 예외를 던져도 마찬가지로 None(폴백 신호).
    """
    try:
        entries = build_index.list_by_dataset(dataset_id, limit=None)
        if not entries and not build_index.list_builds(limit=1):
            return None
    except Exception:
        return None
    return [record for entry in entries if (record := _entry_to_record(entry)) is not None]


def merge_run_records(
    index_records: Sequence[RunRecord], filesystem_records: Sequence[RunRecord]
) -> list[RunRecord]:
    """파생 index와 filesystem 정본 후보를 run_id 기준으로 결정적으로 병합한다.

    같은 run_id가 양쪽에 있으면 snapshot/manifest에서 만든 filesystem record를
    우선하되, filesystem scan만으로 복원할 수 없는 spec_digest는 index 값을
    보존한다. 결과는 입력 순서와 무관하도록 run_id로 정렬한다.
    """
    merged = {record.run_id: record for record in index_records}
    for record in filesystem_records:
        cached = merged.get(record.run_id)
        merged[record.run_id] = RunRecord(
            run_id=record.run_id,
            dataset_id=record.dataset_id,
            status=(
                "cancelled"
                if cached is not None and cached.status == "cancelled"
                else record.status
            ),
            started_at=record.started_at,
            finished_at=record.finished_at,
            spec_digest=(
                cached.spec_digest
                if cached is not None and cached.spec_digest is not None
                else record.spec_digest
            ),
            created_by=record.created_by,
        )
    return [merged[run_id] for run_id in sorted(merged)]


def retain_canonical_run_records(
    output_root: Path, records: Sequence[RunRecord]
) -> list[RunRecord]:
    """snapshot+manifest로 다시 확인되는 run만 남기고 정본 metadata를 적용한다."""
    canonical: list[RunRecord] = []
    for record in records:
        manifest = read_manifest(output_root, record.run_id)
        dataset_id = read_snapshot_dataset_id(output_root, record.run_id)
        if manifest is None or dataset_id is None:
            continue
        started_at = manifest.get("started_at")
        finished_at = manifest.get("finished_at")
        created_by = manifest.get("created_by")
        canonical.append(
            RunRecord(
                run_id=record.run_id,
                dataset_id=dataset_id,
                status=_status_from_manifest(manifest, fallback_status=record.status),
                started_at=started_at if isinstance(started_at, str) else None,
                finished_at=finished_at if isinstance(finished_at, str) else None,
                spec_digest=record.spec_digest,
                created_by=created_by if isinstance(created_by, str) else None,
            )
        )
    return canonical


def _status_from_manifest(
    manifest: dict[str, object], *, fallback_status: str | None = None
) -> str:
    explicit_status = manifest.get("status")
    if isinstance(explicit_status, str) and explicit_status in ("ok", "failed", "cancelled"):
        return explicit_status
    if manifest.get("errors"):
        return "failed"
    return "cancelled" if fallback_status == "cancelled" else "ok"


def collect_run_records_from_filesystem(output_root: Path) -> list[RunRecord]:
    """파일시스템을 직접 스캔해 dataset_id가 있는 run만 RunRecord로 만든다(인덱스 폴백).

    manifest.json이 없거나 buildspec.yaml snapshot에서 dataset_id를 읽을 수 없는
    run(=legacy 또는 손상)은 결과에서 제외한다(#488 semantics B) — 이 run들은
    ``GET /builds``에서는 계속 보이지만 dataset grouping 대상은 아니다.
    """
    if not output_root.exists():
        return []
    records: list[RunRecord] = []
    for run_dir in output_root.iterdir():
        if not run_dir.is_dir():
            continue
        manifest = read_manifest(output_root, run_dir.name)
        if manifest is None:
            continue
        dataset_id = read_snapshot_dataset_id(output_root, run_dir.name)
        if dataset_id is None:
            continue
        started_at = manifest.get("started_at")
        finished_at = manifest.get("finished_at")
        created_by = manifest.get("created_by")
        records.append(
            RunRecord(
                run_id=run_dir.name,
                dataset_id=dataset_id,
                status=_status_from_manifest(manifest),
                started_at=started_at if isinstance(started_at, str) else None,
                finished_at=finished_at if isinstance(finished_at, str) else None,
                spec_digest=None,
                created_by=created_by if isinstance(created_by, str) else None,
            )
        )
    return records


def build_dataset_summary(output_root: Path, record: RunRecord) -> dict[str, JsonValue] | None:
    """latest run의 canonical snapshot + manifest + stage 상태로 dataset 응답을 만든다.

    snapshot을 다시 읽어 dataset_id를 재검증한다 — record.dataset_id는 BuildIndex나
    파일시스템 스캔에서 이미 얻은 값이지만, 최종 응답은 항상 이 시점에 다시 읽은
    canonical snapshot과 일치해야 한다(#488). 재검증에 실패하거나(snapshot이
    사라짐 등) 값이 어긋나면 None을 반환해 호출자가 이 run을 건너뛰게 한다.

    row_count는 단일 스칼라로 축약하지 않는다: multi-source run에서는 source별
    row_counts 맵과 그 합계(total_row_count)를 모두 제공한다(#488 semantics F).

    quality는 항상 None이다 — #486(구조화된 quality gate)을 선반영하지 않으며,
    현재의 log-only 품질 경고를 임의로 PASS/WARN/FAIL로 변환하지 않는다
    (#488 semantics E).
    """
    spec = read_snapshot_spec(output_root, record.run_id)
    if spec is None or spec.dataset_id != record.dataset_id:
        return None
    manifest = read_manifest(output_root, record.run_id) or {}

    sources: list[JsonValue] = [
        {"provider": source.provider, "dataset": source.dataset, "alias": source.alias}
        for source in spec.sources
    ]

    row_counts: dict[str, JsonValue] = {}
    total_row_count = 0
    raw_row_counts = manifest.get("row_counts")
    if isinstance(raw_row_counts, dict):
        for key, value in raw_row_counts.items():
            if isinstance(key, str) and isinstance(value, int):
                row_counts[key] = value
                total_row_count += value

    stage_summaries = list_run_stages(output_root, record.run_id, manifest)
    stages: dict[str, JsonValue] = {
        summary.source_key: {
            "bronze": summary.bronze,
            "silver": summary.silver,
            "gold": summary.gold,
        }
        for summary in stage_summaries
    }

    return {
        "dataset_id": record.dataset_id,
        "title": spec.title,
        "sources": sources,
        "latest_run_id": record.run_id,
        "status": record.status,
        "updated_at": record.finished_at or record.started_at,
        "row_counts": row_counts,
        "total_row_count": total_row_count,
        "stages": stages,
        "quality": None,
    }


__all__ = [
    "RunRecord",
    "build_dataset_summary",
    "collect_run_records_from_filesystem",
    "collect_run_records_from_index",
    "collect_run_records_from_index_for_dataset",
    "filter_ownership",
    "group_latest_by_dataset",
    "is_more_recent",
    "pick_latest",
    "read_manifest",
    "read_snapshot_dataset_id",
    "read_snapshot_spec",
    "merge_run_records",
    "retain_canonical_run_records",
    "sort_key",
]
