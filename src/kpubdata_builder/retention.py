"""취소된 run의 부분 산출물 보존/정리 훅 (#549, ADR 0008 후속).

기본값은 **보존**이다 — cancelled run의 partial 산출물은 감사 증거이므로
아무 설정 없이는 절대 삭제되지 않는다. 정리는 두 겹의 관문 뒤에서만
일어난다:

1. 호출자가 ``apply=True``를 명시적으로 넘겼을 때(CLI ``prune-cancelled
   --apply``). dry-run은 삭제 없이 대상만 나열한다.
2. TTL이 지났을 때 — ``finished_at`` 기준으로 ``ttl_hours``보다 오래된
   cancelled+partial run만 대상이다. TTL 미지정(``None``)이면 대상이
   없다(비활성).

삭제는 run workspace(``{output_root}/{run_id}``) 단위로만 일어나며,
``validate_path_segment``로 run_id를 먼저 검증해 경로 조작을 차단한다.
``_publish_receipts.sqlite``처럼 run 밖에 있는 내부 service 상태는
건드리지 않는다.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .manifest import status_from_manifest
from .stages._path_safety import validate_path_segment

CANCELLED_RUN_TTL_ENV = "KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS"


@dataclass(frozen=True)
class PruneCandidate:
    """정리 대상 후보 run. 삭제 여부와 무관하게 dry-run 보고에도 쓰인다."""

    run_id: str
    finished_at: datetime | None
    partial: bool


@dataclass(frozen=True)
class PruneReport:
    """prune 실행 결과. 삭제는 실제로 일어난 것만 센다."""

    deleted: tuple[str, ...]
    kept: tuple[PruneCandidate, ...]
    scanned: int

    @property
    def deleted_count(self) -> int:
        return len(self.deleted)


def _load_manifest_status(run_dir: Path) -> tuple[str, bool] | None:
    """run workspace의 manifest.json에서 (종단 상태, partial)을 읽는다.

    manifest가 없거나 파싱할 수 없으면 None — 정리 대상 판정에서 제외한다
    (판정 불가 상태를 삭제 대상으로 삼지 않는다, fail-closed).
    """
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    partial = raw.get("partial")
    return status_from_manifest(raw), isinstance(partial, bool) and partial


def _finished_at(run_dir: Path) -> datetime | None:
    manifest_path = run_dir / "manifest.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    finished = raw.get("finished_at")
    if not isinstance(finished, str) or not finished:
        return None
    try:
        parsed = datetime.fromisoformat(finished)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def find_cancelled_partial_runs(output_root: Path) -> list[PruneCandidate]:
    """output_root 아래의 cancelled+partial run을 나열한다 (삭제 없음)."""
    if not output_root.is_dir():
        return []
    candidates: list[PruneCandidate] = []
    for run_dir in sorted(output_root.iterdir()):
        if not run_dir.is_dir():
            continue
        loaded = _load_manifest_status(run_dir)
        if loaded is None:
            continue
        status, partial = loaded
        if status == "cancelled" and partial:
            candidates.append(
                PruneCandidate(run_id=run_dir.name, finished_at=_finished_at(run_dir), partial=True)
            )
    return candidates


def prune_cancelled_runs(
    output_root: Path,
    *,
    ttl_hours: float | None,
    apply: bool = False,
    now: datetime | None = None,
) -> PruneReport:
    """TTL이 지난 cancelled+partial run을 정리한다 (#549).

    매개변수:
        output_root: run workspace 루트.
        ttl_hours: 보존 기간(시간). ``None``이면 비활성 — 무엇도 삭제하지
            않는다. 환경변수 ``KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS``에서
            읽는 것은 호출자(CLI)의 책임이다.
        apply: ``False``(기본)면 dry-run — 대상만 나열하고 삭제하지 않는다.
        now: 판정 기준 시각(테스트 주입). 기본은 현재 UTC.

    반환값:
        삭제된 run_id 목록과 보존된 후보를 담은 :class:`PruneReport`.
    """
    reference = now or datetime.now(timezone.utc)
    candidates = find_cancelled_partial_runs(output_root)

    deleted: list[str] = []
    kept: list[PruneCandidate] = []
    for candidate in candidates:
        if ttl_hours is None:
            kept.append(candidate)
            continue
        finished = candidate.finished_at
        if finished is None:
            # 종료 시각을 알 수 없으면 나이를 판정할 수 없다 — 보존한다.
            kept.append(candidate)
            continue
        if reference - finished < timedelta(hours=ttl_hours):
            kept.append(candidate)
            continue
        if not apply:
            kept.append(candidate)
            continue

        run_id = candidate.run_id
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError:
            # 디렉터리 이름이 run_id 규칙을 벗어나면 어떤 것도 지우지 않는다.
            kept.append(candidate)
            continue
        run_dir = output_root / run_id
        shutil.rmtree(run_dir)
        deleted.append(run_id)

    return PruneReport(
        deleted=tuple(deleted),
        kept=tuple(kept),
        scanned=len(candidates),
    )


__all__ = [
    "CANCELLED_RUN_TTL_ENV",
    "PruneCandidate",
    "PruneReport",
    "find_cancelled_partial_runs",
    "prune_cancelled_runs",
]
