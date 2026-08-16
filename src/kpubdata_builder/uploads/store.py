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

import os
import re
import secrets
import sqlite3
import threading
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from ..spec.models import SOURCE_FILE_FORMATS
from .models import UploadMetadata

MAX_UPLOAD_BYTES_ENV = "KPUBDATA_BUILDER_MAX_UPLOAD_BYTES"
_DEFAULT_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MiB

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

    def __init__(self, path: Path, *, max_bytes: int | None = None) -> None:
        self._path = path
        self._max_bytes = max_bytes if max_bytes is not None else resolve_max_upload_bytes()
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
                    content BLOB NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_uploads_owner_id ON uploads(owner_id)"
            )

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
            raise ValueError(
                f"format must be one of {SOURCE_FILE_FORMATS}, got {format!r}"
            )
        if not content:
            raise ValueError("upload content must not be empty")
        limit = max_bytes if max_bytes is not None else self._max_bytes
        if len(content) > limit:
            raise ValueError(f"upload exceeds max size ({limit} bytes)")

        upload_id = generate_upload_id()
        display_filename = _sanitize_display_filename(original_filename)
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO uploads(
                    upload_id, owner_id, format, encoding, size_bytes,
                    original_filename, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    upload_id,
                    owner_id,
                    format,
                    encoding,
                    len(content),
                    display_filename,
                    content,
                    created_at,
                ),
            )
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
                "SELECT content FROM uploads WHERE owner_id = ? AND upload_id = ?",
                (owner_id, upload_id),
            ).fetchone()
        if row is None:
            return None
        return bytes(row[0])

    def delete(self, owner_id: str, upload_id: str) -> bool:
        self._validate_owner_id(owner_id)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM uploads WHERE owner_id = ? AND upload_id = ?",
                (owner_id, upload_id),
            )
        return cursor.rowcount > 0

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
