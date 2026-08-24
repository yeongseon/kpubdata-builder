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
from contextlib import suppress
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
# 파일시스템 Path로 써서 복사한다(publishers/local.py) — HTTP 노출은
# ``KPUBDATA_BUILDER_LOCAL_PUBLISH_ROOT``로 지정된 publish-root 안의 상대
# 경로로 한정할 때만 허용한다(#550). Kaggle은 packaging이 기록한
# ``dataset-metadata.json``의 ``id``가 destination과 일치할 때만 허용한다
# (#550 정합화 규칙 — 기존 CLI 의미론과 동일).
HTTP_PUBLISH_TARGETS: tuple[str, ...] = ("huggingface", "kaggle", "local")

RunStatus = Literal["queued", "running", "cancelling", "succeeded", "failed", "cancelled"]

# Local publish-root 절대 경로를 지정하는 서버 설정(#550). 미설정이면 local
# target의 readiness가 credential/게시 경계 미구성 blocker를 보고한다(fail-closed).
_LOCAL_PUBLISH_ROOT_ENV = "KPUBDATA_BUILDER_LOCAL_PUBLISH_ROOT"

_DESTINATION_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?/[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
)

# target별로 실제 Publisher.publish()가 받는 kwarg만 허용한다. Hugging Face는
# 신규 repo visibility를 위한 private만, Kaggle은 신규 dataset 공개 여부의
# public만 노출하고 그 밖의 option은 거부한다. Local은 노출 option이 없다.
_ALLOWED_OPTIONS: dict[str, dict[str, type]] = {
    "huggingface": {"private": bool},
    "kaggle": {"public": bool},
    "local": {},
}

_DEFAULT_OPTIONS: dict[str, dict[str, object]] = {
    "huggingface": {"private": True},
    "kaggle": {"public": False},
    "local": {},
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
                # 않는 최소 필드만 append한다. owner_key/run_id를 행에 직접
                # 저장해 receipt가 reset으로 삭제돼도 감사 이력이 조회되게 한다(#563).
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS publish_receipt_audit (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        fingerprint TEXT NOT NULL,
                        owner_key TEXT,
                        run_id TEXT,
                        action TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        recorded_at TEXT NOT NULL
                    )
                    """
                )
                # #557 시점 스키마(owner/run 컬럼 없음)에서 만든 DB 호환 마이그레이션.
                for column in ("owner_key", "run_id"):
                    # 컬럼이 이미 존재하면 ALTER가 OperationalError로 실패한다.
                    with suppress(sqlite3.OperationalError):
                        connection.execute(
                            f"ALTER TABLE publish_receipt_audit ADD COLUMN {column} TEXT"
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
        owner_key, run_id = self._receipt_owner_run(fingerprint)
        self._append_audit(fingerprint, "reconcile_succeeded", owner_key=owner_key, run_id=run_id)

    def reset(self, fingerprint: str, *, action: str = "reset") -> bool:
        """receipt를 삭제해 재시도(새 claim)를 허용한다(#551). 감사 로그를 남긴다.

        삭제가 실제로 일어났으면 True, 해당 fingerprint가 없으면 False.
        감사 행에는 receipt의 owner/run을 옮겨 적는다(#563) — receipt 삭제 후에도
        이력이 소유자·run 단위로 조회되어야 하기 때문이다.
        """
        self._initialize()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_key, run_id FROM publish_receipts WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if row is None:
                connection.commit()
                return False
            owner_key, run_id = cast(str | None, row[0]), cast(str, row[1])
            connection.execute(
                "DELETE FROM publish_receipts WHERE fingerprint = ?",
                (fingerprint,),
            )
            self._append_audit_on(
                connection, fingerprint, action, owner_key=owner_key, run_id=run_id
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _receipt_owner_run(self, fingerprint: str) -> tuple[str | None, str | None]:
        """fingerprint에서 receipt의 (owner_key, run_id)를 읽는다(#563)."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT owner_key, run_id FROM publish_receipts WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        if row is None:
            return None, None
        return cast(str | None, row[0]), cast(str | None, row[1])

    def _append_audit(
        self,
        fingerprint: str,
        action: str,
        *,
        owner_key: str | None = None,
        run_id: str | None = None,
        actor: str = "operator",
    ) -> None:
        with self._connect() as connection:
            self._append_audit_on(
                connection, fingerprint, action, owner_key=owner_key, run_id=run_id, actor=actor
            )

    @staticmethod
    def _append_audit_on(
        connection: sqlite3.Connection,
        fingerprint: str,
        action: str,
        *,
        owner_key: str | None,
        run_id: str | None,
        actor: str = "operator",
    ) -> None:
        recorded_at = datetime_module.now(timezone.utc).isoformat(timespec="seconds")
        connection.execute(
            """
            INSERT INTO publish_receipt_audit(
                fingerprint, owner_key, run_id, action, actor, recorded_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (fingerprint, owner_key, run_id, action, actor, recorded_at),
        )

    def audit_entries(self, *, owner_key: str, run_id: str) -> list[dict[str, str]]:
        """해당 owner/run의 감사 로그를 시간순으로 반환한다.

        receipt가 삭제(reset)된 이후의 행도 포함한다(#563) — 감사 행이 owner/run을
        스스로 들고 있으므로 JOIN이 필요 없다.
        """
        self._initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT fingerprint, action, actor, recorded_at
                FROM publish_receipt_audit
                WHERE owner_key = ? AND run_id = ?
                ORDER BY seq
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
        # BEGIN IMMEDIATE로 전환 순간까지 쓰기 잠금을 잡아 claim()과 같은
        # 직렬화 수준에서 상태 전이를 확정한다(#564).
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT state FROM publish_receipts WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if current is None or cast(str, current[0]) not in allowed_source_states:
                connection.commit()
                raise RuntimeError(
                    "publish receipt is not in an allowed state for this transition"
                    f" (allowed: {list(allowed_source_states)})"
                )
            connection.execute(
                """
                UPDATE publish_receipts
                SET state = ?, result_json = ?
                WHERE fingerprint = ?
                """,
                (state, result_json, fingerprint),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


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


def _kaggle_credential_configured() -> bool:
    return bool(os.environ.get("KAGGLE_USERNAME")) and bool(os.environ.get("KAGGLE_KEY"))


def local_publish_root() -> Path | None:
    """설정된 local publish-root 절대 경로(#550). 미설정/불완전이면 None."""
    raw = os.environ.get(_LOCAL_PUBLISH_ROOT_ENV, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def resolve_local_destination(destination: str) -> tuple[Path, Path] | PublishIssue:
    """local target의 destination을 publish-root 안의 절대 경로로 해석한다.

    반환값: ``(publish_root, absolute_destination)`` 또는 blocker.
    destination은 항상 상대 ``owner/name`` 형태여야 하고, root를 벗어나는
    경로는 fail-closed다(#550).
    """
    root = local_publish_root()
    if root is None:
        return PublishIssue(
            "local_publish_root_unconfigured",
            "KPUBDATA_BUILDER_LOCAL_PUBLISH_ROOT is not configured on the server",
        )
    absolute = (root / destination).resolve()
    try:
        ensure_within(root, absolute, label="local publish destination")
    except ValueError:
        return PublishIssue(
            "destination_outside_publish_root",
            "destination must stay inside the configured local publish root",
        )
    return root, absolute


def kaggle_package_id(artifacts: ResolvedArtifacts) -> tuple[Path, str] | PublishIssue | None:
    """Kaggle packaging을 해석한다 (#550).

    반환값:
        ``(package_dir, metadata_id)`` — 정확히 하나의 dataset-metadata.json이
        있고 id를 읽을 수 있을 때(KagglePublisher는 디렉터리 artifact를
        받는다). ``None`` — packaging이 아예 없을 때. :class:`PublishIssue` —
        packaging이 여러 개(모호)거나 id를 읽을 수 없을 때.
    """
    # dataset-metadata.json은 exporter가 기록하는 sidecar라 manifest.outputs에
    # 들어가지 않는다 — 해석된 gold artifact의 형제 디렉터리에서 찾는다(#550).
    package_dirs: list[Path] = []
    for artifact_path in artifacts.paths:
        candidate = artifact_path.parent / "dataset-metadata.json"
        if candidate.is_file() and candidate.parent not in package_dirs:
            package_dirs.append(candidate.parent)
    if not package_dirs:
        return None
    if len(package_dirs) > 1:
        return PublishIssue(
            "kaggle_metadata_ambiguous",
            "run has multiple Kaggle packagings; publish target is ambiguous",
        )
    metadata_path = package_dirs[0] / "dataset-metadata.json"
    try:
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return PublishIssue(
            "kaggle_metadata_unreadable",
            "Kaggle dataset-metadata.json could not be read",
        )
    if not isinstance(raw, dict):
        return PublishIssue(
            "kaggle_metadata_unreadable",
            "Kaggle dataset-metadata.json could not be read",
        )
    metadata_id = raw.get("id")
    if not isinstance(metadata_id, str) or not metadata_id:
        return PublishIssue(
            "kaggle_metadata_unreadable",
            "Kaggle dataset-metadata.json has no dataset id",
        )
    return metadata_path.parent, metadata_id


def credential_blocker(target: str) -> PublishIssue | None:
    """target publish credential/경계가 서버에 설정돼 있는지만 확인한다.

    원문 credential은 절대 읽거나 반환하지 않는다. 설정 여부를 확인할 수 없으면
    (지원 불가능한 credential shape 포함) ready=true로 추정하지 않고 명시적으로
    unavailable로 처리한다(#491 지침 7). local target의 "credential"은
    publish-root 설정이다(#550).
    """
    if target == "huggingface" and _huggingface_credential_configured():
        return None
    if target == "kaggle" and _kaggle_credential_configured():
        return None
    if target == "local" and local_publish_root() is not None:
        return None

    if target == "local":
        return PublishIssue(
            "local_publish_root_unconfigured",
            "no local publish root is configured for target 'local'",
        )
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
    destination: str,
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

            if target == "kaggle":
                # Kaggle packaging의 dataset-metadata.json id가 destination과
                # 일치해야 하고(#550), KagglePublisher는 패키지 디렉터리를
                # 받으므로 artifact 묶음을 디렉터리 형태로 바꾼다. packaging
                # 부재는 destination과 무관한 blocker지만 id 불일치 검사는
                # destination이 주어진 경우에만 한다(GET readiness는 선택
                # 파라미터, POST가 최종 재검증한다).
                package = kaggle_package_id(resolved)
                if isinstance(package, PublishIssue):
                    blockers.append(package)
                elif package is None:
                    blockers.append(
                        PublishIssue(
                            "kaggle_metadata_missing",
                            "run has no Kaggle packaging (dataset-metadata.json) to publish",
                        )
                    )
                else:
                    package_dir, metadata_id = package
                    if destination and metadata_id != destination:
                        blockers.append(
                            PublishIssue(
                                "kaggle_destination_mismatch",
                                f"packaged Kaggle dataset id {metadata_id!r} does not match"
                                f" destination {destination!r}",
                            )
                        )
                    else:
                        artifacts = ResolvedArtifacts(paths=(package_dir,), expects_directory=True)

            if target == "local" and destination:
                resolved_local = resolve_local_destination(destination)
                if isinstance(resolved_local, PublishIssue):
                    blockers.append(resolved_local)

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
