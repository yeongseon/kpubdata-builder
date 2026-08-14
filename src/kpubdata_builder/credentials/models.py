"""Provider credential 도메인 모델."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CredentialMetadata:
    """원문 credential을 포함하지 않는 저장 메타데이터."""

    provider: str
    configured: bool
    masked: str | None
    updated_at: str | None


__all__ = ["CredentialMetadata"]
