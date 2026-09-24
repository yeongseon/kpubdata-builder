"""Provider 도메인 서비스 (#596 첫 조각).

``BuilderService`` 가 providers/uploads/query/builds/datasets/quality 를 한 클래스에
전부 들고 있던 구조를 도메인별로 나누는 작업의 첫 단계다. 여기서 확립하는 형태를
나머지 도메인이 그대로 따르면 된다.

핵심 규칙 두 가지:
    - **자기 의존성만 받는다.** ``BuilderService`` 전체를 주입받지 않는다 — 그러면
      클래스만 늘고 결합은 그대로다. provider 도메인이 실제로 쓰는 것은 credential
      resolver, client 팩토리, provider test 설정 세 가지뿐이다.
    - **wire 계약은 그대로다.** 반환 타입(``ServiceResponse``)·상태 코드·본문 키가
      바뀌지 않는다. 라우팅과 인증 게이트는 손대지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import cast

from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.providers import (
    CredentialResolver,
    ProviderDescriptor,
    ProviderTestOperation,
    provider_descriptors,
    run_provider_test,
    test_result_body,
)
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.bronze.build import SourceClient

# principal/providers/timeout 을 받아 요청 단위 client 를 만드는 팩토리.
# BuilderService._create_client 의 시그니처를 그대로 따른다.
CreateClient = Callable[..., SourceClient]
CloseClient = Callable[[SourceClient | None], None]


def _raise_provider_test_error(client: SourceClient, provider: str) -> None:
    """client 생성 자체가 실패한 경우에도 원문 예외를 노출하지 않는다."""
    del client, provider
    raise RuntimeError("provider test unavailable")


class ProvidersService:
    """Provider 목록·연결 테스트·credential CRUD."""

    def __init__(
        self,
        *,
        credential_resolver: CredentialResolver,
        create_client: CreateClient,
        close_client: CloseClient,
        provider_test_operation: ProviderTestOperation,
        provider_test_timeout: float,
    ) -> None:
        self._credential_resolver = credential_resolver
        self._create_client = create_client
        self._close_client = close_client
        self._provider_test_operation = provider_test_operation
        self._provider_test_timeout = provider_test_timeout

    # --- 내부 조회 ---------------------------------------------------------

    def runtime_providers(self) -> tuple[ProviderDescriptor, ...] | ServiceResponse:
        """런타임 provider 카탈로그. 조회 실패는 502 로 변환한다."""
        client = self._create_client()
        try:
            return provider_descriptors(client)
        except Exception:
            # upstream 클라이언트의 예외 문자열에는 요청 URL 이 섞여 나올 수 있고,
            # data.go.kr 계열은 API 키를 쿼리 파라미터로 실어 보낸다 — 그대로
            # 응답에 넣으면 남의 키가 에러 메시지로 새어 나간다.
            _logger_exception("provider catalog unavailable")
            return ServiceResponse(502, {"error": "catalog unavailable"})
        finally:
            self._close_client(client)

    def known_provider(self, provider: str) -> ProviderDescriptor | ServiceResponse:
        providers = self.runtime_providers()
        if isinstance(providers, ServiceResponse):
            return providers
        match = next((item for item in providers if item.name == provider), None)
        if match is None:
            return ServiceResponse(404, {"error": "provider not found"})
        return match

    # --- 공개 엔드포인트 ---------------------------------------------------

    def providers(self, *, principal: Principal) -> ServiceResponse:
        """런타임 Provider 목록과 현재 principal의 configured 상태를 반환한다."""
        descriptors = self.runtime_providers()
        if isinstance(descriptors, ServiceResponse):
            return descriptors
        if principal.owner_id is None:
            return ServiceResponse(403, {"error": "stable principal is required"})
        items: list[JsonValue] = []
        for descriptor in descriptors:
            resolved = self._credential_resolver.resolve(principal.owner_id, descriptor.name)
            configured = not descriptor.requires_credential or resolved.value is not None
            items.append(
                {
                    "provider": descriptor.name,
                    "requires_credential": descriptor.requires_credential,
                    "configured": configured,
                }
            )
        return ServiceResponse(200, {"providers": items})

    def provider_status(self, provider: str, *, principal: Principal) -> ServiceResponse:
        """현재 principal credential로 lightweight connection test를 수행한다."""
        descriptor = self.known_provider(provider)
        if isinstance(descriptor, ServiceResponse):
            return descriptor
        if principal.owner_id is None:
            return ServiceResponse(403, {"error": "stable principal is required"})
        resolved = self._credential_resolver.resolve(principal.owner_id, provider)
        configured = not descriptor.requires_credential or resolved.value is not None
        client: SourceClient | None = None
        try:
            if configured:
                client = self._create_client(
                    principal, providers=(provider,), timeout=self._provider_test_timeout
                )
            result = run_provider_test(
                provider=provider,
                configured=configured,
                client=client,
                operation=self._provider_test_operation,
            )
            return ServiceResponse(200, cast(dict[str, JsonValue], test_result_body(result)))
        except Exception:
            # Client 생성 실패도 원문 예외를 로그/응답하지 않고 unknown으로 제한한다.
            result = run_provider_test(
                provider=provider,
                configured=True,
                client=cast(SourceClient, object()),
                operation=_raise_provider_test_error,
            )
            return ServiceResponse(200, cast(dict[str, JsonValue], test_result_body(result)))
        finally:
            if client is not None:
                self._close_client(client)

    def provider_credential(self, provider: str, *, principal: Principal) -> ServiceResponse:
        """원문 없이 현재 principal의 저장 credential 메타데이터를 반환한다."""
        known = self.known_provider(provider)
        if isinstance(known, ServiceResponse):
            return known
        repository = self._credential_resolver.repository
        if repository is None:
            return ServiceResponse(503, {"error": "credential store is not configured"})
        if principal.owner_id is None:
            return ServiceResponse(403, {"error": "stable principal is required"})
        metadata = repository.get_metadata(principal.owner_id, provider)
        return ServiceResponse(
            200,
            {
                "configured": metadata.configured,
                "masked": metadata.masked,
                "updated_at": metadata.updated_at,
            },
        )

    def put_provider_credential(
        self,
        provider: str,
        body: Mapping[str, JsonValue] | None,
        *,
        principal: Principal,
    ) -> ServiceResponse:
        """현재 principal의 Provider credential을 생성 또는 교체한다."""
        known = self.known_provider(provider)
        if isinstance(known, ServiceResponse):
            return known
        repository = self._credential_resolver.repository
        if repository is None:
            return ServiceResponse(503, {"error": "credential store is not configured"})
        if principal.owner_id is None:
            return ServiceResponse(403, {"error": "stable principal is required"})
        if body is None or set(body) != {"credential"}:
            return ServiceResponse(400, {"error": "body must contain only 'credential'"})
        credential = body.get("credential")
        if not isinstance(credential, str) or not credential.strip():
            return ServiceResponse(400, {"error": "credential must be a non-empty string"})
        metadata = repository.put(principal.owner_id, provider, credential)
        return ServiceResponse(
            200,
            {
                "provider": metadata.provider,
                "configured": metadata.configured,
                "masked": metadata.masked,
                "updated_at": metadata.updated_at,
            },
        )

    def delete_provider_credential(self, provider: str, *, principal: Principal) -> ServiceResponse:
        """현재 principal의 Provider credential만 삭제한다."""
        known = self.known_provider(provider)
        if isinstance(known, ServiceResponse):
            return known
        repository = self._credential_resolver.repository
        if repository is None:
            return ServiceResponse(503, {"error": "credential store is not configured"})
        if principal.owner_id is None:
            return ServiceResponse(403, {"error": "stable principal is required"})
        _ = repository.delete(principal.owner_id, provider)
        return ServiceResponse(
            200,
            {"provider": provider, "configured": False, "masked": None, "updated_at": None},
        )

    def provider_keys(self, owner_id: str | None, providers: Iterable[str]) -> Mapping[str, str]:
        """빌드 실행이 쓰는 principal별 provider key 해석 (얇은 위임)."""
        return self._credential_resolver.provider_keys(owner_id, tuple(providers))


def _logger_exception(message: str) -> None:
    import logging

    logging.getLogger("kpubdata_builder.service").exception(message)


__all__ = ["ProvidersService"]
