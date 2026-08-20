"""Build publish readiness/실행 서비스 로직 (#491).

Studio가 완료된 Gold build를 바로 게시하지 않고, readiness(ready/blockers/
warnings)를 먼저 확인한 뒤 실제 publish를 요청할 수 있게 하는 순수 로직을
담는다. 새 Publisher는 만들지 않는다 — HTTP에서는
``publishers.PUBLISHER_REGISTRY``의 기존 Hugging Face publisher만 재사용한다.
Kaggle/Local registry와 CLI 동작은 유지하되 HTTP target에서는 제외한다(#28/#491).

핵심 원칙:
    - readiness(GET)는 side-effect-free다: Publisher를 호출하거나 원격
      dataset을 만들지 않는다.
    - POST도 readiness와 완전히 같은 deterministic 검사를 서버에서 다시
      수행한다 — 호출자가 GET을 먼저 불렀다고 신뢰하지 않는다(TOCTOU 방지).
    - Publisher에 넘기는 artifact 목록은 항상 ``manifest.outputs``(정본,
      pipeline이 실제로 쓴 파일만 기록)와 ``gold_source_dir``(정본 stage
      경로 helper, #488)의 교집합에서만 만든다 — 임의 디렉터리를 glob하지
      않는다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, replace
from datetime import datetime as datetime_module
from datetime import timezone
from pathlib import Path
from typing import Literal, cast

from ..errors import ValidationError
from ..publishers import PUBLISHER_REGISTRY
from ..spec import BuildSpec
from ..spec.validator import validate_spec
from ..stages._path_safety import ensure_within
from ..stages._stage_reader import gold_source_dir
from . import stages as stages_service

# HTTP로 안전하게 노출 가능한 publish target. PUBLISHER_REGISTRY에는 "local"도
# 있지만, LocalPublisher는 caller-provided destination을 그대로 로컬
# 파일시스템 Path로 써서 복사한다(publishers/local.py) — 이 저장소에는 그
# destination을 안전하게 한정할 configured publish-root/allowlist 계약이
# 아직 없다(검색 확인: publish_root/local publish 관련 서버 설정이 없음).
# 그런 계약이 생기기 전까지 "local"을 HTTP publish target에서 제외한다.
# Kaggle도 정상 pipeline packaging의 metadata.id가 destination-aware해질 때까지
# 제외한다. PUBLISHER_REGISTRY에 있다는 사실만으로 HTTP에 안전하고 실제로
# publish 가능하다고 가정하지 않는다(#491).
HTTP_PUBLISH_TARGETS: tuple[str, ...] = ("huggingface",)

RunStatus = Literal["queued", "running", "cancelling", "succeeded", "failed", "cancelled"]

_DESTINATION_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?/[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
)

# target별로 실제 Publisher.publish()가 받는 kwarg만 허용한다. Hugging Face는
# 신규 repo visibility를 위한 private만 노출하고 그 밖의 option은 거부한다.
_ALLOWED_OPTIONS: dict[str, dict[str, type]] = {
    "huggingface": {"private": bool},
}

_DEFAULT_OPTIONS: dict[str, dict[str, object]] = {
    "huggingface": {"private": True},
}

PublishReceiptState = Literal["pending", "succeeded", "unknown"]
PublishClaimStatus = Literal["claimed", "replay", "in_progress", "state_unknown", "conflict"]


@dataclass(frozen=True)
class PublishReceipt:
    """credential/path를 포함하지 않는 durable publish operation 영수증."""

    fingerprint: str
    state: PublishReceiptState
    target: str
    destination: str
    options: dict[str, object]
    result: dict[str, object] | None


class PublishReceiptStore:
    """SQLite UNIQUE claim으로 duplicate remote side effect를 막는 receipt 저장소.

    DB는 ``output_root/_publish_receipts.sqlite``에 있으며 run workspace 밖의 내부
    service state다. artifact API는 run 디렉터리만 열거하므로 public artifact나
    ``manifest.outputs``에 포함되지 않는다.
    """

    _FILENAME = "_publish_receipts.sqlite"

    def __init__(self, output_root: Path) -> None:
        self.path = output_root / self._FILENAME
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS publish_receipts (
                        owner_key TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        target TEXT NOT NULL,
                        destination TEXT NOT NULL,
                        fingerprint TEXT NOT NULL UNIQUE,
                        options_json TEXT NOT NULL,
                        state TEXT NOT NULL
                            CHECK (state IN ('pending', 'succeeded', 'unknown')),
                        result_json TEXT,
                        PRIMARY KEY (owner_key, run_id, target, destination)
                    )
                    """
                )
                # reconcile/reset 감사 로그(#551) — credential/path/원문을 담지
                # 않는 최소 필드만 append한다.
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS publish_receipt_audit (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        fingerprint TEXT NOT NULL,
                        action TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        recorded_at TEXT NOT NULL
                    )
                    """
                )
            self._initialized = True

    @staticmethod
    def fingerprint(
        *,
        owner_key: str,
        run_id: str,
        target: str,
        destination: str,
        options: dict[str, object],
    ) -> str:
        canonical = json.dumps(
            {
                "owner": owner_key,
                "run_id": run_id,
                "target": target,
                "destination": destination,
                "options": options,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    @staticmethod
    def _row_to_receipt(row: tuple[object, ...]) -> PublishReceipt:
        options = json.loads(cast(str, row[4]))
        result = json.loads(cast(str, row[6])) if row[6] is not None else None
        if not isinstance(options, dict) or (result is not None and not isinstance(result, dict)):
            raise ValueError("invalid publish receipt JSON")
        return PublishReceipt(
            fingerprint=cast(str, row[0]),
            state=cast(PublishReceiptState, row[5]),
            target=cast(str, row[2]),
            destination=cast(str, row[3]),
            options=cast(dict[str, object], options),
            result=cast(dict[str, object] | None, result),
        )

    def claim(
        self,
        *,
        owner_key: str,
        run_id: str,
        target: str,
        destination: str,
        options: dict[str, object],
    ) -> tuple[PublishClaimStatus, PublishReceipt]:
        """operation을 durable pending으로 선점하거나 기존 상태를 반환한다."""
        self._initialize()
        fingerprint = self.fingerprint(
            owner_key=owner_key,
            run_id=run_id,
            target=target,
            destination=destination,
            options=options,
        )
        options_json = json.dumps(
            options, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT fingerprint, owner_key, target, destination, options_json, state,
                       result_json
                FROM publish_receipts
                WHERE owner_key = ? AND run_id = ? AND target = ? AND destination = ?
                """,
                (owner_key, run_id, target, destination),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO publish_receipts(
                        owner_key, run_id, target, destination, fingerprint,
                        options_json, state, result_json
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL)
                    """,
                    (owner_key, run_id, target, destination, fingerprint, options_json),
                )
                connection.commit()
                return (
                    "claimed",
                    PublishReceipt(
                        fingerprint=fingerprint,
                        state="pending",
                        target=target,
                        destination=destination,
                        options=dict(options),
                        result=None,
                    ),
                )

            receipt = self._row_to_receipt(cast(tuple[object, ...], row))
            connection.commit()
            if receipt.fingerprint != fingerprint:
                return "conflict", receipt
            if receipt.state == "succeeded":
                return "replay", receipt
            if receipt.state == "unknown":
                return "state_unknown", receipt
            return "in_progress", receipt
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def lookup(
        self,
        *,
        owner_key: str,
        run_id: str,
        target: str,
        destination: str,
        options: dict[str, object],
    ) -> tuple[PublishClaimStatus, PublishReceipt] | None:
        """side effect 없이 기존 operation receipt와 replay 결정을 조회한다."""
        self._initialize()
        fingerprint = self.fingerprint(
            owner_key=owner_key,
            run_id=run_id,
            target=target,
            destination=destination,
            options=options,
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT fingerprint, owner_key, target, destination, options_json, state,
                       result_json
                FROM publish_receipts
                WHERE owner_key = ? AND run_id = ? AND target = ? AND destination = ?
                """,
                (owner_key, run_id, target, destination),
            ).fetchone()
        if row is None:
            return None
        receipt = self._row_to_receipt(cast(tuple[object, ...], row))
        if receipt.fingerprint != fingerprint:
            return "conflict", receipt
        if receipt.state == "succeeded":
            return "replay", receipt
        if receipt.state == "unknown":
            return "state_unknown", receipt
        return "in_progress", receipt

    def mark_succeeded(self, fingerprint: str, result: dict[str, object]) -> None:
        self._set_terminal(fingerprint, state="succeeded", result=result)

    def mark_unknown(self, fingerprint: str) -> None:
        self._set_terminal(fingerprint, state="unknown", result=None)

    def get_by_key(
        self,
        *,
        owner_key: str,
        run_id: str,
        target: str,
        destination: str,
    ) -> PublishReceipt | None:
        """operation key로 receipt를 직접 조회한다(#551 조회 API용, side effect 없음)."""
        self._initialize()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT fingerprint, owner_key, target, destination, options_json, state,
                       result_json
                FROM publish_receipts
                WHERE owner_key = ? AND run_id = ? AND target = ? AND destination = ?
                """,
                (owner_key, run_id, target, destination),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_receipt(cast(tuple[object, ...], row))

    def reconcile_succeeded(self, fingerprint: str, result: dict[str, object]) -> None:
        """원격 상태 확인으로 unknown을 succeeded로 확정한다(#551). 감사 로그를 남긴다."""
        self._initialize()
        self._set_terminal(
            fingerprint,
            state="succeeded",
            result=result,
            allowed_source_states=("pending", "unknown"),
        )
        self._append_audit(fingerprint, "reconcile_succeeded")

    def reset(self, fingerprint: str, *, action: str = "reset") -> bool:
        """receipt를 삭제해 재시도(새 claim)를 허용한다(#551). 감사 로그를 남긴다.

        삭제가 실제로 일어났으면 True, 해당 fingerprint가 없으면 False.
        """
        self._initialize()
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM publish_receipts WHERE fingerprint = ?",
                (fingerprint,),
            )
            deleted = cursor.rowcount == 1
        if deleted:
            self._append_audit(fingerprint, action)
        return deleted

    def _append_audit(self, fingerprint: str, action: str, *, actor: str = "operator") -> None:
        recorded_at = datetime_module.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO publish_receipt_audit(fingerprint, action, actor, recorded_at)
                VALUES (?, ?, ?, ?)
                """,
                (fingerprint, action, actor, recorded_at),
            )

    def audit_entries(self, *, owner_key: str, run_id: str) -> list[dict[str, str]]:
        """해당 owner/run의 감사 로그를 시간순으로 반환한다(조회 API용)."""
        self._initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT a.fingerprint, a.action, a.actor, a.recorded_at
                FROM publish_receipt_audit a
                JOIN publish_receipts r ON r.fingerprint = a.fingerprint
                WHERE r.owner_key = ? AND r.run_id = ?
                ORDER BY a.seq
                """,
                (owner_key, run_id),
            ).fetchall()
        return [
            {
                "fingerprint": cast(str, row[0]),
                "action": cast(str, row[1]),
                "actor": cast(str, row[2]),
                "recorded_at": cast(str, row[3]),
            }
            for row in rows
        ]

    def _set_terminal(
        self,
        fingerprint: str,
        *,
        state: Literal["succeeded", "unknown"],
        result: dict[str, object] | None,
        allowed_source_states: tuple[str, ...] = ("pending",),
    ) -> None:
        self._initialize()
        result_json = (
            json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if result is not None
            else None
        )
        placeholders = ", ".join("?" for _ in allowed_source_states)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE publish_receipts
                SET state = ?, result_json = ?
                WHERE fingerprint = ? AND state IN ({placeholders})
                """,
                (state, result_json, fingerprint, *allowed_source_states),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("publish receipt is not pending")


@dataclass(frozen=True)
class PublishIssue:
    """구조화된 blocker/warning. UI가 문자열 파싱을 하지 않도록 code+message로 나눈다."""

    code: str
    message: str

    def to_body(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class ResolvedArtifacts:
    """target에 실제로 전달할 canonical Gold artifact 경로.

    ``paths``는 항상 manifest.outputs(정본)에 실제로 기록된, 그리고 이 run의
    gold_source_dir 아래에 있는 파일만 담는다 — BuildSpec snapshot, credential,
    임시 파일, silver/bronze 파일은 절대 섞이지 않는다.
    """

    paths: tuple[Path, ...]
    expects_directory: bool


@dataclass(frozen=True)
class ReadinessResult:
    target: str
    ready: bool
    blockers: tuple[PublishIssue, ...]
    warnings: tuple[PublishIssue, ...]
    artifacts: ResolvedArtifacts | None = None


def resolve_target(value: object) -> tuple[str | None, str | None]:
    """(target, error_message) — target이 None이면 error_message가 채워진다."""
    if not isinstance(value, str) or not value:
        return None, "'target' must be a non-empty string"
    if value in HTTP_PUBLISH_TARGETS:
        return value, None
    if value in PUBLISHER_REGISTRY:
        return None, f"target {value!r} is not available over the publish HTTP API"
    return None, f"unknown publish target: {value!r}"


def run_status_blocker(status: RunStatus) -> PublishIssue | None:
    """Run 상태 자체가 publish를 막는지 판정한다. 기존 상태 어휘만 쓴다."""
    if status in ("queued", "running", "cancelling"):
        return PublishIssue("run_not_terminal", f"run is not finished yet (status={status})")
    if status == "failed":
        return PublishIssue("run_failed", "run finished with errors and cannot be published")
    if status == "cancelled":
        return PublishIssue("run_cancelled", "run was cancelled and cannot be published")
    return None


def license_blocker(spec: BuildSpec | None) -> PublishIssue | None:
    """#443 license/redistribution gate를 재사용한다 — 새 license 정책을 만들지 않는다.

    BuildSpec.license가 선언되어 있지 않으면(빈 문자열 포함) publish를 막는다.
    unknown license를 자동 허용하지 않는다 — 선언 자체가 유일한 재배포
    가능성 근거다(#443 원칙 그대로).

    ``spec.license``가 whitespace만으로 이루어진 문자열(spec loader는 이를
    타입 검사만 하고 통과시킨다, spec/loader.py)이면 사람이 실제로 아무것도
    선언하지 않은 것과 같으므로 "선언됨"으로 인정하지 않는다(#491 지침 4) —
    새 SPDX allowlist/registry는 추가하지 않는다, blank 판정만 보강한다.
    """
    if spec is None or not spec.license or not spec.license.strip():
        return PublishIssue(
            "license_missing",
            "BuildSpec.license must be declared before this dataset can be published (#443)",
        )
    return None


def effective_publish_policy_blockers(spec: BuildSpec | None) -> tuple[PublishIssue, ...]:
    """저장된 spec을 실제 외부 publish와 같은 ``publish=True``로 재검증한다.

    PII/license 규칙을 service에 복제하지 않고 canonical ``validate_spec``의
    structured problems를 사용한다. whitespace-only license는 validator의 기존
    truthiness 검사보다 엄격한 #491 gate인 ``license_blocker``가 보완한다.
    """
    if spec is None:
        return ()
    try:
        validate_spec(replace(spec, publish=True))
    except ValidationError as exc:
        structured = exc.structured_problems or ()
        return tuple(
            PublishIssue(problem.code, problem.message)
            for problem in structured
            if problem.code != "missing_license_for_publish"
        )
    return ()


def _huggingface_credential_configured() -> bool:
    return bool(os.environ.get("HF_TOKEN"))


def credential_blocker(target: str) -> PublishIssue | None:
    """target publish credential이 서버에 설정돼 있는지만 boolean으로 확인한다.

    원문 credential은 절대 읽거나 반환하지 않는다. 설정 여부를 확인할 수 없으면
    (지원 불가능한 credential shape 포함) ready=true로 추정하지 않고 명시적으로
    unavailable로 처리한다(#491 지침 7).
    """
    configured = _huggingface_credential_configured()
    if configured:
        return None
    return PublishIssue(
        "credential_unavailable",
        f"no server-side credential is configured for target {target!r}",
    )


def resolve_gold_artifacts(
    output_root: Path, run_id: str, manifest: dict[str, object]
) -> ResolvedArtifacts | PublishIssue:
    """이 run의 canonical Gold artifact 파일을 해석한다.

    ``manifest.outputs``에 실제로 기록된 파일 중, 알려진(실패하지 않은)
    source의 ``gold_source_dir`` 아래에 있는 파일만 후보로 삼는다(#488 stage
    helper 재사용) — output_root를 recursive glob하지 않는다.

    manifest.outputs에는 gold 산출물뿐 아니라 이 run의 bronze/silver 원본,
    dataset card, BuildSpec snapshot 등 다른 stage의 정당한 산출물도 함께
    기록된다(pipeline/orchestrator.py `_record_output_paths` 호출부 참고) —
    이런 항목은 gold_source_dir 밖에 있는 것이 "정상"이므로 조용히 후보에서
    제외한다(publish 대상은 gold 파일만). 반면 canonical manifest.outputs
    항목이 이 run 자신의 workspace(``{output_root}/{run_id}``) 밖을
    가리키면(gold root escape, symlink escape, invalid/resolve 불가 경로
    포함) — 그건 정당한 다른 stage 산출물일 수 없으므로 fail-closed다(#491
    지침 2). 유효한 gold artifact가 함께 있어도 그 무효 항목만 조용히
    건너뛰고 나머지만 publish하지 않는다 — canonical publish artifact set
    전체가 유효해야 한다.
    """
    known = stages_service.known_source_keys(manifest)
    failed = stages_service.failed_source_keys(manifest)
    candidate_sources = [key for key in known if key not in failed]

    outputs_raw = manifest.get("outputs")
    output_paths = (
        [Path(p) for p in outputs_raw if isinstance(p, str)]
        if isinstance(outputs_raw, list)
        else []
    )

    run_dir = output_root / run_id

    gold_dirs: list[Path] = []
    for source_key in candidate_sources:
        try:
            gold_dir = gold_source_dir(output_root, run_id, source_key)
        except ValueError:
            continue
        if gold_dir.is_dir():
            gold_dirs.append(gold_dir)

    if not gold_dirs:
        return PublishIssue(
            "gold_unavailable", "no successful Gold output is available for this run"
        )

    # 모든 output path를 이 run에 실제로 존재하는 gold_dir 전체에 대해
    # 먼저 분류한다(source별로 nested 검사하지 않는다) — 그래야 source A의
    # gold_dir과 비교할 때 source B 소유 경로가 "무효"로 오판되지 않는다.
    gold_files: list[Path] = []
    for path in output_paths:
        matched = False
        for gold_dir in gold_dirs:
            try:
                ensure_within(gold_dir, path, label="gold artifact")
            except ValueError:
                continue
            matched = True
            break
        if matched:
            if not path.is_file():
                return PublishIssue(
                    "artifact_missing",
                    f"expected Gold artifact is missing on disk: {path.name}",
                )
            gold_files.append(path)
            continue

        try:
            ensure_within(run_dir, path, label="run output")
        except ValueError:
            # 이 run 자신의 workspace 밖을 가리키는 canonical output이다
            # (경로 정책 위반, symlink escape, resolve 불가 포함) — 정당한
            # 다른 stage 산출물일 수 없으므로 조용히 건너뛰지 않고 즉시
            # fail-closed 처리한다.
            return PublishIssue(
                "artifact_invalid",
                "a canonical manifest output failed the path-safety check and cannot be published",
            )
        # run_dir 안에는 있지만 어느 gold_dir에도 속하지 않는다 — bronze/
        # silver 산출물, dataset card, BuildSpec snapshot 등 정당한 비-gold
        # 산출물이다. publish 대상이 아니므로(gold만) 조용히 제외한다.

    if not gold_files:
        return PublishIssue(
            "gold_unavailable", "no successful Gold output is available for this run"
        )

    unique_sorted_files = sorted(set(gold_files))

    return ResolvedArtifacts(paths=tuple(unique_sorted_files), expects_directory=False)


def validate_destination(target: str, destination: object) -> str | None:
    """target이 요구하는 canonical 'owner/name' identifier 형태만 허용한다.

    filesystem path로 절대 해석하지 않는다 — URL, scheme, 절대/상대 경로,
    상위 이동(``..``), 제어 문자, 앞뒤 공백을 모두 거부한다(#491 지침 6).
    """
    if not isinstance(destination, str):
        return "'destination' must be a string"
    if not destination:
        return "'destination' must not be empty"
    if destination != destination.strip():
        return "'destination' must not have leading/trailing whitespace"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in destination):
        return "'destination' must not contain control characters"
    if "://" in destination:
        return "'destination' must not be a URL"
    if destination.startswith(("/", "\\")):
        return "'destination' must not be an absolute path"
    if ".." in destination.replace("\\", "/").split("/"):
        return "'destination' must not contain path traversal segments"
    if not _DESTINATION_PATTERN.match(destination):
        return f"'destination' must look like 'owner/name' for target {target!r}"
    return None


def validate_options(target: str, options: object) -> tuple[str | None, dict[str, object]]:
    """(error_message, normalized_options). 미지원 option은 조용히 무시하지 않는다."""
    if options is None:
        return None, dict(_DEFAULT_OPTIONS.get(target, {}))
    if not isinstance(options, dict):
        return "'options' must be an object", {}
    allowed = _ALLOWED_OPTIONS.get(target, {})
    for key, value in options.items():
        if not isinstance(key, str) or key not in allowed:
            return f"unsupported option for target {target!r}: {key!r}", {}
        expected_type = allowed[key]
        if expected_type is bool and not isinstance(value, bool):
            return f"option {key!r} must be a boolean", {}
    normalized = dict(_DEFAULT_OPTIONS.get(target, {}))
    normalized.update(options)
    return None, normalized


def build_readiness(
    *,
    run_id: str,
    target: str,
    status: RunStatus,
    manifest: dict[str, object] | None,
    spec: BuildSpec | None,
    output_root: Path,
) -> ReadinessResult:
    """readiness/POST가 공유하는 단일 deterministic 판정.

    ``ready``는 항상 ``not blockers``다 — 별도 계산 경로가 없다. GET과 POST
    모두 이 함수 하나만 호출해 같은 결론에 도달한다(TOCTOU 재검증, #491 지침 3/4).
    """
    blockers: list[PublishIssue] = []

    status_issue = run_status_blocker(status)
    if status_issue is not None:
        blockers.append(status_issue)

    artifacts: ResolvedArtifacts | None = None
    if manifest is None:
        if status_issue is None:
            # 비정상 상태: terminal(succeeded)인데 manifest가 없음 — fail-closed.
            blockers.append(PublishIssue("gold_unavailable", "run manifest is unavailable"))
    else:
        resolved = resolve_gold_artifacts(output_root, run_id, manifest)
        if isinstance(resolved, PublishIssue):
            blockers.append(resolved)
        else:
            artifacts = resolved

        blockers.extend(effective_publish_policy_blockers(spec))

        license_issue = license_blocker(spec)
        if license_issue is not None:
            blockers.append(license_issue)

    credential_issue = credential_blocker(target)
    if credential_issue is not None:
        blockers.append(credential_issue)

    return ReadinessResult(
        target=target,
        ready=not blockers,
        blockers=tuple(blockers),
        warnings=(),
        artifacts=artifacts,
    )


__all__ = [
    "HTTP_PUBLISH_TARGETS",
    "PublishClaimStatus",
    "PublishIssue",
    "PublishReceipt",
    "PublishReceiptStore",
    "ReadinessResult",
    "ResolvedArtifacts",
    "RunStatus",
    "build_readiness",
    "credential_blocker",
    "effective_publish_policy_blockers",
    "license_blocker",
    "resolve_gold_artifacts",
    "resolve_target",
    "run_status_blocker",
    "validate_destination",
    "validate_options",
]
