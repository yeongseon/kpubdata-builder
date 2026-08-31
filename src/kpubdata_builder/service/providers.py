"""Provider credential resolution과 connection test 서비스."""

from __future__ import annotations

import socket
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol, cast

from kpubdata import Client
from kpubdata.config import KPubDataConfig
from kpubdata.exceptions import (
    AuthError,
    ConfigError,
    ProviderResponseError,
    PublicDataError,
    TransportError,
    TransportTimeoutError,
)

from ..credentials import CredentialMetadata, CredentialRepository
from ..stages.bronze.build import SourceClient

CredentialSource = Literal["user", "server", "none"]
ProviderState = Literal["connected", "failed", "not_configured"]
ProviderErrorCategory = Literal["auth", "network", "timeout", "provider", "unknown"]

# kpubdata가 동일 data.go.kr key slot을 쓰는 Provider 이름. Builder에서는 API key를
# 요청 경로의 provider로 저장하되 요청별 Client 생성 시 공개 provider_keys slot으로 변환한다.
_CLIENT_KEY_SLOT: dict[str, str] = {
    "localdata": "datago",
    "lofin": "datago",
    "semas": "datago",
}


@dataclass(frozen=True)
class ResolvedCredential:
    """해석된 credential. API 응답 모델로 사용하지 않는다."""

    source: CredentialSource
    value: str | None


@dataclass(frozen=True)
class ProviderDescriptor:
    """런타임 Provider 메타데이터."""

    name: str
    requires_credential: bool


@dataclass(frozen=True)
class ProviderTestResult:
    """원문 예외/credential을 포함하지 않는 connection test 결과."""

    provider: str
    status: ProviderState
    configured: bool
    latency_ms: int
    checked_at: str
    error_category: ProviderErrorCategory | None = None
    response_code: int | None = None


class ProviderCredentialConflictError(ValueError):
    """한 Client key slot에 서로 다른 user credential이 필요한 경우."""


class ProviderTestOperation(Protocol):
    """주입 가능한 lightweight connection test operation."""

    def __call__(self, client: SourceClient, provider: str) -> None: ...


class CredentialResolver:
    """user credential > server default > not configured 순서를 단일화한다."""

    def __init__(self, repository: CredentialRepository | None) -> None:
        self._repository = repository

    @property
    def repository(self) -> CredentialRepository | None:
        return self._repository

    @staticmethod
    def client_key_slot(provider: str) -> str:
        return _CLIENT_KEY_SLOT.get(provider, provider)

    def resolve(self, owner_id: str | None, provider: str) -> ResolvedCredential:
        if self._repository is not None and owner_id is not None:
            user_value = self._repository.get_secret(owner_id, provider)
            if user_value is not None:
                return ResolvedCredential("user", user_value)
        server_value = KPubDataConfig.from_env().get_provider_key(self.client_key_slot(provider))
        if server_value:
            return ResolvedCredential("server", server_value)
        return ResolvedCredential("none", None)

    def provider_keys(self, owner_id: str | None, providers: Iterable[str]) -> dict[str, str]:
        """요청에 필요한 provider만 해석해 새 Client용 key mapping을 만든다."""
        resolved: dict[str, str] = {}
        for provider in providers:
            credential = self.resolve(owner_id, provider)
            if credential.value is None:
                continue
            slot = self.client_key_slot(provider)
            previous = resolved.get(slot)
            if previous is not None and previous != credential.value:
                raise ProviderCredentialConflictError(
                    f"providers sharing credential slot {slot!r} have conflicting credentials"
                )
            resolved[slot] = credential.value
        return resolved

    def metadata(self, owner_id: str, provider: str) -> CredentialMetadata:
        if self._repository is None:
            return CredentialMetadata(provider, False, None, None)
        return self._repository.get_metadata(owner_id, provider)


def provider_descriptors(client: SourceClient) -> tuple[ProviderDescriptor, ...]:
    """kpubdata 공개 runtime catalog에서 Provider 목록과 인증 필요 여부를 얻는다."""
    typed_client = cast(Client, client)
    # Built-in adapters are registered lazily.  Resolve them one at a time so an
    # optional dependency of one adapter (for example KRX -> pandas) cannot make
    # the catalog for every other provider unavailable.
    registry = getattr(typed_client, "_registry", None)
    if registry is None:
        authenticated = frozenset(p.name for p in typed_client.iter_authenticated_providers())
        names = sorted({dataset.provider for dataset in typed_client.datasets.list()})
        return tuple(ProviderDescriptor(name, name in authenticated) for name in names)

    descriptors: list[ProviderDescriptor] = []
    for name in registry:
        try:
            adapter = registry.get(name)
            typed_client.datasets.list(provider=name)
        except ModuleNotFoundError as exc:
            # KRX is the sole built-in provider with this optional dependency.
            # Do not hide a missing internal module or another provider bug.
            if name == "krx" and exc.name == "pandas":
                continue
            raise
        descriptors.append(
            ProviderDescriptor(name, bool(getattr(adapter, "requires_api_key", True)))
        )
    return tuple(descriptors)


def default_provider_test(client: SourceClient, provider: str) -> None:
    """Provider의 첫 LIST dataset을 1행 조회하는 lightweight test."""
    typed_client = cast(Client, client)
    refs = [ref for ref in typed_client.datasets.list() if ref.provider == provider]
    if not refs:
        raise ValueError("unknown provider")
    dataset_ref = refs[0]
    _ = typed_client.dataset(dataset_ref.id).list(page=1, page_size=1)


def run_provider_test(
    *,
    provider: str,
    configured: bool,
    client: SourceClient | None,
    operation: ProviderTestOperation = default_provider_test,
) -> ProviderTestResult:
    """Connection test를 실행하고 안정적인 범주의 비밀 없는 결과로 변환한다."""
    started = time.perf_counter()
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not configured or client is None:
        return ProviderTestResult(provider, "not_configured", False, 0, checked_at)
    try:
        operation(client, provider)
    except Exception as exc:
        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        category = categorize_provider_error(exc)
        response_code = reliable_response_code(exc)
        return ProviderTestResult(
            provider,
            "failed",
            True,
            latency_ms,
            checked_at,
            error_category=category,
            response_code=response_code,
        )
    latency_ms = max(0, round((time.perf_counter() - started) * 1000))
    return ProviderTestResult(provider, "connected", True, latency_ms, checked_at)


def categorize_provider_error(exc: Exception) -> ProviderErrorCategory:
    """kpubdata/stdlib 예외를 Issue #492 error category로 매핑한다."""
    if isinstance(exc, (TransportTimeoutError, TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, (AuthError, ConfigError)):
        return "auth"
    if isinstance(exc, TransportError):
        return "network"
    if isinstance(exc, (ProviderResponseError, PublicDataError)):
        return "provider"
    return "unknown"


def reliable_response_code(exc: Exception) -> int | None:
    """kpubdata가 구조적으로 제공한 HTTP status만 반환한다."""
    if not isinstance(exc, PublicDataError):
        return None
    status_code = exc.status_code
    return status_code if isinstance(status_code, int) and 100 <= status_code <= 599 else None


def test_result_body(result: ProviderTestResult) -> dict[str, object]:
    """optional 필드를 성공적으로 얻은 경우에만 포함하는 wire body."""
    body: dict[str, object] = {
        "provider": result.provider,
        "status": result.status,
        "configured": result.configured,
        "latency_ms": result.latency_ms,
        "checked_at": result.checked_at,
    }
    if result.error_category is not None:
        body["error_category"] = result.error_category
    if result.response_code is not None:
        body["response_code"] = result.response_code
    return body


__all__ = [
    "CredentialResolver",
    "ProviderCredentialConflictError",
    "ProviderDescriptor",
    "ProviderTestOperation",
    "ProviderTestResult",
    "categorize_provider_error",
    "default_provider_test",
    "provider_descriptors",
    "reliable_response_code",
    "run_provider_test",
    "test_result_body",
]
