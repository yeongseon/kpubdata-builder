"""SQLite 기반 업로드 저장소 (#498).

``credentials/store.py``(#492)와 같은 모양을 따른다 — 단일 SQLite 파일에
``(owner_id, upload_id)`` 로 격리된 row를 저장한다. credential과 달리 업로드
content는 secret이 아니므로 암호화하지 않지만, 다음 두 가지로 안전한 staging을
보장한다(#498):

    - ``upload_id`` 는 서버가 ``secrets`` 로 생성한다 — 사용자가 filename이나
      path를 직접 지정해 파일시스템을 조작할 수 없다(파일 자체를 filesystem에
      쓰지 않고 SQLite BLOB으로 저장하므로 path traversal 표면이 아예 없다).
    - 모든 조회/삭제는 ``owner_id`` 로 scoping된다 — 다른 사용자의 upload는
      존재 여부조차 구분되지 않고 동일하게 "not found"로 취급된다(fail-closed,
      #505의 ownership 패턴과 동일).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import sqlite3
import tempfile
import threading
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from ..spec.models import SOURCE_FILE_FORMATS
from ..stages._path_safety import ensure_within
from .models import UploadMetadata


class UploadContentCorrupted(RuntimeError):
    """저장된 payload 를 읽을 수 없거나 기록된 체크섬과 다르다 (#622).

    조용히 빈 값이나 다른 바이트를 돌려주면, 그것으로 만든 Bronze 가 무엇이었는지
    아무도 알 수 없게 된다.
    """


MAX_UPLOAD_BYTES_ENV = "KPUBDATA_BUILDER_MAX_UPLOAD_BYTES"
_DEFAULT_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MiB

#: 이 크기부터 payload 를 BLOB 대신 파일로 쓴다 (#622).
#:
#: SQLite 의 기본 ``SQLITE_MAX_LENGTH`` 는 약 953 MiB 다. 그 한계에 닿아 실패한
#: 실제 사례가 있었는데(945.6 MiB 는 통과, 1,444 MiB 는 ``string or blob too
#: big``), 문제는 한계의 크기가 아니라 **업로드 상한이 SQLite 의 단일 value 한계
#: 라는 구현 세부로 우연히 정해졌다** 는 점이다. 상한은 정책으로 정해야 한다
#: (``KPUBDATA_BUILDER_MAX_UPLOAD_BYTES``).
#:
#: 8 MiB 로 잡은 것은 한계에서 한참 떨어뜨리기 위해서다. 작은 업로드는 예전처럼
#: BLOB 으로 남아 기존 동작이 그대로다 — 기본 상한(20 MiB)보다 작으므로 큰 쪽만
#: 새 경로를 탄다.
_DEFAULT_SPILL_THRESHOLD_BYTES = 8 * 1024 * 1024  # 8 MiB

# 표시 전용 원본 filename의 최대 보존 길이. 파일시스템 경로로는 절대 쓰이지
# 않으므로 위험한 문자 자체를 막을 필요는 없지만, 응답 크기를 bound한다.
_MAX_DISPLAY_FILENAME_LENGTH = 255


def resolve_max_upload_bytes() -> int:
    """업로드 크기 상한을 환경변수에서 읽는다 (없거나 잘못되면 기본값)."""
    raw = os.environ.get(MAX_UPLOAD_BYTES_ENV, "").strip()
    if not raw:
        return _DEFAULT_MAX_UPLOAD_BYTES
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_UPLOAD_BYTES
    return value if value > 0 else _DEFAULT_MAX_UPLOAD_BYTES


def generate_upload_id() -> str:
    """``spec.models.UPLOAD_ID_PATTERN`` 과 항상 일치하는 새 upload_id를 만든다.

    ``token_hex(16)`` 은 항상 32자리 소문자 16진수 문자열을 반환하므로
    ``UPLOAD_ID_PATTERN``(``^upl_[a-f0-9]{32}$``)과 구조적으로 항상 일치한다.
    """
    return f"upl_{secrets.token_hex(16)}"


def _sanitize_display_filename(original_filename: str | None) -> str | None:
    """원본 filename을 표시 전용 문자열로 안전화한다.

    파일시스템 경로로 쓰지 않으므로 traversal 방지가 목적이 아니라, 경로
    구분자를 남겨 응답을 읽는 클라이언트가 실수로 path처럼 다루지 않도록
    basename만 남기고 길이를 제한한다.
    """
    if original_filename is None:
        return None
    stripped = original_filename.strip()
    if not stripped:
        return None
    basename = re.split(r"[\\/]", stripped)[-1]
    return basename[:_MAX_DISPLAY_FILENAME_LENGTH] or None


class UploadRepository(Protocol):
    """owner_id + upload_id를 key로 하는 업로드 저장소 abstraction."""

    def put(
        self,
        owner_id: str,
        *,
        content: bytes,
        format: str,  # noqa: A002 - 계약 필드명과 맞춘다
        encoding: str,
        original_filename: str | None,
        max_bytes: int | None = None,
    ) -> UploadMetadata: ...

    def get_metadata(self, owner_id: str, upload_id: str) -> UploadMetadata | None: ...

    def get_content(self, owner_id: str, upload_id: str) -> bytes | None: ...

    def delete(self, owner_id: str, upload_id: str) -> bool: ...

    def list_for_owner(self, owner_id: str) -> Sequence[UploadMetadata]: ...


class SQLiteUploadRepository:
    """content를 BLOB으로 저장하는 SQLite 업로드 저장소."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int | None = None,
        spill_threshold_bytes: int = _DEFAULT_SPILL_THRESHOLD_BYTES,
    ) -> None:
        self._path = path
        self._max_bytes = max_bytes if max_bytes is not None else resolve_max_upload_bytes()
        self._spill_threshold = spill_threshold_bytes
        # 파일로 나간 payload 가 사는 곳. DB 파일 옆에 두어 두 상태가 함께
        # 백업·이동되게 한다.
        self._blob_root = path.parent / f"{path.name}.blobs"
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS uploads (
                    upload_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    format TEXT NOT NULL,
                    encoding TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    original_filename TEXT,
                    content BLOB,
                    created_at TEXT NOT NULL,
                    -- 큰 payload 는 BLOB 대신 파일로 나간다 (#622). 둘 중 정확히
                    -- 하나만 채워진다.
                    blob_path TEXT,
                    content_sha256 TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_uploads_owner_id ON uploads(owner_id)"
            )
            # 기존 DB 마이그레이션. 컬럼 추가는 idempotent 하지 않으므로 존재를 본다.
            existing = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(uploads)").fetchall()
            }
            for column in ("blob_path", "content_sha256"):
                if column not in existing:
                    connection.execute(f"ALTER TABLE uploads ADD COLUMN {column} TEXT")

    @staticmethod
    def _validate_owner_id(owner_id: str) -> None:
        if not owner_id:
            raise ValueError("stable owner_id is required")

    def put(
        self,
        owner_id: str,
        *,
        content: bytes,
        format: str,  # noqa: A002 - 계약 필드명과 맞춘다
        encoding: str,
        original_filename: str | None,
        max_bytes: int | None = None,
    ) -> UploadMetadata:
        self._validate_owner_id(owner_id)
        if format not in SOURCE_FILE_FORMATS:
            raise ValueError(f"format must be one of {SOURCE_FILE_FORMATS}, got {format!r}")
        if not content:
            raise ValueError("upload content must not be empty")
        limit = max_bytes if max_bytes is not None else self._max_bytes
        if len(content) > limit:
            raise ValueError(f"upload exceeds max size ({limit} bytes)")

        upload_id = generate_upload_id()
        display_filename = _sanitize_display_filename(original_filename)
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        blob: bytes | None = content
        blob_path: str | None = None
        digest: str | None = None
        if len(content) >= self._spill_threshold:
            blob = None
            digest = hashlib.sha256(content).hexdigest()
            blob_path = self._write_blob(upload_id, content)

        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO uploads(
                        upload_id, owner_id, format, encoding, size_bytes,
                        original_filename, content, created_at, blob_path, content_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        upload_id,
                        owner_id,
                        format,
                        encoding,
                        len(content),
                        display_filename,
                        blob,
                        created_at,
                        blob_path,
                        digest,
                    ),
                )
            except BaseException:
                # row 가 없으면 파일에 닿을 방법이 없다 — 고아를 남기지 않는다.
                if blob_path is not None:
                    self._remove_blob(blob_path)
                raise
        return UploadMetadata(
            upload_id=upload_id,
            format=format,
            encoding=encoding,
            size_bytes=len(content),
            original_filename=display_filename,
            created_at=created_at,
        )

    def get_metadata(self, owner_id: str, upload_id: str) -> UploadMetadata | None:
        self._validate_owner_id(owner_id)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT format, encoding, size_bytes, original_filename, created_at
                FROM uploads WHERE owner_id = ? AND upload_id = ?
                """,
                (owner_id, upload_id),
            ).fetchone()
        if row is None:
            return None
        return UploadMetadata(
            upload_id=upload_id,
            format=str(row[0]),
            encoding=str(row[1]),
            size_bytes=int(row[2]),
            original_filename=str(row[3]) if row[3] is not None else None,
            created_at=str(row[4]),
        )

    def get_content(self, owner_id: str, upload_id: str) -> bytes | None:
        self._validate_owner_id(owner_id)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT content, blob_path, content_sha256
                FROM uploads WHERE owner_id = ? AND upload_id = ?
                """,
                (owner_id, upload_id),
            ).fetchone()
        if row is None:
            return None
        if row[1] is None:
            return bytes(row[0])
        content = self._read_blob(str(row[1]))
        expected = str(row[2]) if row[2] is not None else None
        if expected is not None and hashlib.sha256(content).hexdigest() != expected:
            # 디스크의 payload 가 기록된 것과 다르다. 조용히 돌려주면 그 바이트로
            # 만든 Bronze 가 무엇이었는지 아무도 알 수 없다.
            raise UploadContentCorrupted(
                f"stored content for {upload_id} does not match its recorded checksum"
            )
        return content

    def delete(self, owner_id: str, upload_id: str) -> bool:
        self._validate_owner_id(owner_id)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT blob_path FROM uploads WHERE owner_id = ? AND upload_id = ?",
                (owner_id, upload_id),
            ).fetchone()
            cursor = connection.execute(
                "DELETE FROM uploads WHERE owner_id = ? AND upload_id = ?",
                (owner_id, upload_id),
            )
            deleted = cursor.rowcount > 0
        # row 를 지운 뒤에 파일을 지운다. 순서가 반대면 삭제가 중간에 실패했을 때
        # row 는 남고 payload 만 사라진다.
        if deleted and row is not None and row[0] is not None:
            self._remove_blob(str(row[0]))
        return deleted

    # --- file-backed payload (#622) ---------------------------------------

    def _blob_file(self, relative: str) -> Path:
        """상대 경로를 blob 루트 안의 절대 경로로 바꾼다.

        경로는 ``upload_id`` 에서만 만들어지고 그 형식은 이미 검증돼 있지만,
        DB 에서 읽은 값을 그대로 믿지 않는다 — 저장소가 어떻게든 오염되면 경로가
        루트 밖을 가리킬 수 있다.
        """
        candidate = (self._blob_root / relative).resolve()
        ensure_within(self._blob_root.resolve(), candidate, label="upload blob")
        return candidate

    def _write_blob(self, upload_id: str, content: bytes) -> str:
        relative = f"{upload_id}.bin"
        destination = self._blob_file(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # 임시 파일에 쓰고 교체한다 — 부분 파일이 완성본으로 읽히지 않는다.
        fd, tmp_name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            os.replace(tmp_name, destination)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        return relative

    def _read_blob(self, relative: str) -> bytes:
        try:
            return self._blob_file(relative).read_bytes()
        except OSError as exc:
            raise UploadContentCorrupted(
                f"stored upload payload is unreadable: {relative}"
            ) from exc

    def _remove_blob(self, relative: str) -> None:
        with contextlib.suppress(OSError, ValueError):
            self._blob_file(relative).unlink(missing_ok=True)

    def list_for_owner(self, owner_id: str) -> Sequence[UploadMetadata]:
        self._validate_owner_id(owner_id)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT upload_id, format, encoding, size_bytes, original_filename, created_at
                FROM uploads WHERE owner_id = ? ORDER BY created_at DESC
                """,
                (owner_id,),
            ).fetchall()
        return tuple(
            UploadMetadata(
                upload_id=str(row[0]),
                format=str(row[1]),
                encoding=str(row[2]),
                size_bytes=int(row[3]),
                original_filename=str(row[4]) if row[4] is not None else None,
                created_at=str(row[5]),
            )
            for row in rows
        )


__all__ = [
    "MAX_UPLOAD_BYTES_ENV",
    "SQLiteUploadRepository",
    "UploadRepository",
    "generate_upload_id",
    "resolve_max_upload_bytes",
]
