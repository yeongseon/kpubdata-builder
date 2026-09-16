"""CUBRID 기반 암호화 Provider credential 저장소 (ADR 0016).

``SQLiteCredentialRepository`` 와 동일한 ``CredentialRepository`` Protocol 을 SQLAlchemy
Core 로 구현한다. **ciphertext 만** 저장하며, AES-GCM AAD(``associated_data``)와 owner
검증(``validate_owner_id``)은 store.py 의 단일 함수를 공유한다 — 백엔드가 달라도
암복호 시맨틱이 동일하다.

이 모듈은 ``_credential_repository_from_env`` 의 cubrid 분기에서만 import 된다 —
``sqlalchemy`` 를 import 하므로 기본(sqlite) 경로에 optional 의존성을 끌어들이지 않는다.

동시성: 프로세스 전역 단일 Engine(커넥션 풀 + pool_pre_ping)을 받아 연산마다 짧은
커넥션을 빌린다. put 은 dialect 독립적으로 단일 트랜잭션 내 delete+insert 로 upsert 한다.
credential 쓰기는 사용자 액션이므로 (파생 인덱스와 달리) 예외를 삼키지 않고 전파한다.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    Column,
    MetaData,
    String,
    Table,
    Text,
    delete,
    insert,
    select,
)

from .crypto import CredentialCipher
from .models import CredentialMetadata
from .store import _MASK, associated_data, normalize_provider, validate_owner_id

if TYPE_CHECKING:
    from sqlalchemy import Engine


class CubridCredentialRepository:
    """ciphertext 만 CUBRID 에 기록하는 credential repository (ADR 0016)."""

    def __init__(self, engine: Engine, cipher: CredentialCipher) -> None:
        self._engine = engine
        self._cipher = cipher
        self._metadata = MetaData()
        self._table = Table(
            "provider_credentials",
            self._metadata,
            Column("owner_id", String(255), primary_key=True),
            Column("provider", String(64), primary_key=True),
            # ciphertext 는 base64 텍스트(CLOB)로 저장한다. CUBRID 는 BLOB 에 NOT NULL 을
            # 허용하지 않고(errno -1014), pycubrid 의 BLOB 바이너리 왕복이 str 로 돌아와
            # 깨지므로, base64 문자열로 저장해 결정적 왕복 + NOT NULL 을 확보한다. SQLite
            # 구현은 raw BLOB 을 쓰지만, Protocol 뒤라 저장 표현은 백엔드마다 달라도 된다.
            Column("ciphertext", Text, nullable=False),
            Column("updated_at", String(40), nullable=False),
        )
        self._table.create(self._engine, checkfirst=True)

    def get_metadata(self, owner_id: str, provider: str) -> CredentialMetadata:
        validate_owner_id(owner_id)
        provider = normalize_provider(provider)
        stmt = select(self._table.c.updated_at).where(
            self._table.c.owner_id == owner_id, self._table.c.provider == provider
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        if row is None:
            return CredentialMetadata(provider, False, None, None)
        return CredentialMetadata(provider, True, _MASK, str(row[0]))

    def get_secret(self, owner_id: str, provider: str) -> str | None:
        validate_owner_id(owner_id)
        provider = normalize_provider(provider)
        stmt = select(self._table.c.ciphertext).where(
            self._table.c.owner_id == owner_id, self._table.c.provider == provider
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        if row is None:
            return None
        ciphertext = base64.b64decode(row[0])
        return self._cipher.decrypt(ciphertext, associated_data=associated_data(owner_id, provider))

    def list_configured_providers(self, owner_id: str) -> Sequence[str]:
        validate_owner_id(owner_id)
        stmt = (
            select(self._table.c.provider)
            .where(self._table.c.owner_id == owner_id)
            .order_by(self._table.c.provider)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return tuple(str(row[0]) for row in rows)

    def put(self, owner_id: str, provider: str, credential: str) -> CredentialMetadata:
        validate_owner_id(owner_id)
        provider = normalize_provider(provider)
        if not credential or not credential.strip():
            raise ValueError("credential must be a non-empty string")
        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ciphertext = self._cipher.encrypt(
            credential, associated_data=associated_data(owner_id, provider)
        )
        ciphertext_b64 = base64.b64encode(ciphertext).decode("ascii")
        # 단일 트랜잭션 내 delete+insert — dialect upsert 에 의존하지 않는다.
        with self._engine.begin() as conn:
            conn.execute(
                delete(self._table).where(
                    self._table.c.owner_id == owner_id, self._table.c.provider == provider
                )
            )
            conn.execute(
                insert(self._table).values(
                    owner_id=owner_id,
                    provider=provider,
                    ciphertext=ciphertext_b64,
                    updated_at=updated_at,
                )
            )
        return CredentialMetadata(provider, True, _MASK, updated_at)

    def delete(self, owner_id: str, provider: str) -> bool:
        validate_owner_id(owner_id)
        provider = normalize_provider(provider)
        stmt = delete(self._table).where(
            self._table.c.owner_id == owner_id, self._table.c.provider == provider
        )
        with self._engine.begin() as conn:
            result = conn.execute(stmt)
        return bool(result.rowcount and result.rowcount > 0)


__all__ = ["CubridCredentialRepository"]
