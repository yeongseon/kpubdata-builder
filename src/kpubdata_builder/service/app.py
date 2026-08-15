"""Builder 서비스 로직 (#36).

Studio 같은 외부 UI가 Builder를 호출할 수 있도록 validate/preview/build/artifacts
연산을 HTTP 전송과 분리된 순수 로직으로 제공한다. 각 메서드는 ServiceResponse
(상태 코드 + JSON 직렬화 가능한 body)를 반환하며, dispatch가 (method, path)를
해당 연산으로 라우팅한다.

주요 구성:
    - ServiceResponse: 상태 코드 + body
    - BuilderService: validate/preview/build/artifacts 연산
    - dispatch: 경로 라우팅
"""

from __future__ import annotations

import heapq
import inspect
import json
import logging
import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

import yaml
from kpubdata import Client
from kpubdata.core.models import DatasetRef
from typing_extensions import assert_never

from ..credentials import (
    AesGcmCredentialCipher,
    CredentialRepository,
    SQLiteCredentialRepository,
)
from ..errors import SpecLoadError, ValidationError
from ..pipeline import preview_build, run_build
from ..quality import QualityCheckResult
from ..query.engine import QueryExecutionError, QueryTimeoutError
from ..query.models import QueryRequest, QueryStage
from ..query.resolver import (
    QueryArtifactUnavailableError,
    QueryContextError,
    resolve_query_context,
)
from ..query.security import UnsafeQueryError, validate_read_only_sql
from ..query.service import QueryBusyError, QueryService
from ..spec import BuildSpec, JsonValue, parse_spec
from ..spec.serializer import BUILDSPEC_SNAPSHOT_FILENAME, compute_spec_digest
from ..spec.validator import validate_spec
from ..stages._path_safety import ensure_within, validate_path_segment
from ..stages.bronze.build import SourceClient
from ..store import BuildIndex
from ..tabular import DEFAULT_PREVIEW_LIMIT
from . import datasets as datasets_service
from . import ownership as ownership_module
from . import quality as quality_service
from . import stages as stages_service
from .auth import AuthError, Principal, authenticate
from .jobs import AsyncBuildExecutor, generate_run_id
from .providers import (
    CredentialResolver,
    ProviderCredentialConflictError,
    ProviderDescriptor,
    ProviderTestOperation,
    default_provider_test,
    provider_descriptors,
    run_provider_test,
    test_result_body,
)
from .responses import FileResponse, ServiceResponse
from .routes import ROUTE_ADAPTERS

logger = logging.getLogger(__name__)

_CREDENTIAL_MASTER_KEY_ENV = "KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY"
_PROVIDER_TEST_TIMEOUT_ENV = "KPUBDATA_BUILDER_PROVIDER_TEST_TIMEOUT"
_DEFAULT_PROVIDER_TEST_TIMEOUT = 10.0


@runtime_checkable
class _CloseableClient(Protocol):
    def close(self) -> None: ...


def _close_request_client(client: SourceClient) -> None:
    if isinstance(client, _CloseableClient):
        client.close()


def _credential_repository_from_env(output_root: Path) -> CredentialRepository | None:
    """master key가 설정된 경우에만 encrypted repository를 활성화한다."""
    encoded_key = os.environ.get(_CREDENTIAL_MASTER_KEY_ENV)
    if not encoded_key:
        return None
    cipher = AesGcmCredentialCipher.from_base64(encoded_key)
    return SQLiteCredentialRepository(
        output_root / ".service" / "provider-credentials.sqlite3", cipher
    )


def _factory_accepts_keyword(factory: Callable[..., SourceClient], keyword: str) -> bool:
    """factory가 특정 keyword 또는 **kwargs를 받는지 side effect 없이 판정한다."""
    try:
        parameters = inspect.signature(factory).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD or parameter.name == keyword
        for parameter in parameters
    )


def _redact_secret_text(value: str | None, secrets: Iterable[str]) -> str | None:
    """응답/manifest 문자열에서 현재 요청 credential 원문을 제거한다."""
    if value is None:
        return None
    redacted = value
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _redact_json_secrets(value: object, secrets: Iterable[str]) -> object:
    """JSON tree의 모든 문자열에서 credential을 재귀적으로 제거한다."""
    secret_values = tuple(secrets)
    if isinstance(value, str):
        return _redact_secret_text(value, secret_values)
    if isinstance(value, list):
        return [_redact_json_secrets(item, secret_values) for item in value]
    if isinstance(value, dict):
        return {key: _redact_json_secrets(item, secret_values) for key, item in value.items()}
    return value


def _authenticated_provider_names(client: SourceClient) -> frozenset[str]:
    authenticated_providers = cast(Client, client).iter_authenticated_providers()
    return frozenset(provider.name for provider in authenticated_providers)


def _requires_service_key(dataset: DatasetRef, auth_provider_names: frozenset[str]) -> bool:
    return dataset.provider in auth_provider_names or bool(
        dataset.raw_metadata.get("service_key_param")
    )


def _enforce_ownership() -> bool:
    """run 소유권 강제가 활성화되어 있는지 (#389). 기본 off — 하위 호환."""
    return ownership_module.enforce_ownership()


# Build list entry type for API responses
_BuildListEntry = dict[str, str | None]


def _apply_ownership(
    entries: list[_BuildListEntry], principal: Principal | None
) -> list[_BuildListEntry]:
    """list_builds 응답에서 본인 소유 run만 남긴다 (#433, #505).

    ENFORCE_OWNERSHIP+oidc principal일 때만 필터링. dev/service principal과
    principal=None은 통과 (관리자 권한 + 하위 호환). 인덱스 분기와 파일시스템
    폴백 양쪽에서 공통으로 적용해 폴백 경로가 필터를 우회하지 않게 한다.

    각 entry는 판정용으로 내부 전용 "owner_id" 키를 담고 있어야 한다 — 응답
    직전에 ``_strip_internal_fields``로 제거되므로 wire 응답 shape는 바뀌지
    않는다.
    """
    if not (_enforce_ownership() and principal and principal.kind == "oidc"):
        return entries
    return [
        e
        for e in entries
        if ownership_module.ownership_allows(
            created_by=e.get("created_by"),
            owner_id=e.get("owner_id"),
            principal=principal,
            enforce=True,
        )
    ]


def _strip_internal_fields(entries: list[_BuildListEntry]) -> list[_BuildListEntry]:
    """ownership 판정에만 쓰인 내부 전용 필드를 응답 직전에 제거한다 (#505).

    ``owner_id``는 canonical hash일 뿐 클라이언트에 의미가 없고, 이를 노출하면
    ``/builds`` 응답 wire shape가 바뀐다 — 계약 변경 없이 내부적으로만 쓴다.
    """
    return [{k: v for k, v in e.items() if k != "owner_id"} for e in entries]


# Builder API 계약 버전. contract/builder-api.yaml의 info.version과 일치해야 하며
# (test_service_contract가 강제), 응답에 실어 Studio 같은 소비자가 하위 호환을
# 협상할 수 있게 한다 (#209).
# 1.4.0 -> 1.5.0: Dataset Catalog·Detail·Stage Summary API 추가 (#488, additive).
# 1.5.0 -> 1.6.0: 구조화된 Quality/Schema Drift 결과, quality history/detail API 추가
# (#486, additive — 기존 엔드포인트는 변경되지 않는다).
# 1.8.0 -> 1.9.0: query startup/engine timing fields 추가 (#523, additive).
API_CONTRACT_VERSION = "1.9.0"


def _quality_result_to_json(r: QualityCheckResult) -> dict[str, JsonValue]:
    """QualityCheckResult를 wire JSON으로 변환한다 (#486)."""
    return {
        "source_key": r.source_key,
        "category": r.category,
        "rule": r.rule,
        "column": r.column,
        "status": r.status,
        "actual": cast(JsonValue, r.actual),
        "threshold": r.threshold,
        "affected_rows": r.affected_rows,
        "evaluated_rows": r.evaluated_rows,
        "detail": r.detail,
    }


def _parse_spec_text(spec_yaml: str) -> BuildSpec:
    """YAML 텍스트를 BuildSpec으로 파싱한다."""
    raw = cast(object, yaml.safe_load(spec_yaml))
    if not isinstance(raw, dict):
        raise SpecLoadError("top-level YAML must be a mapping")
    return parse_spec(cast(dict[str, object], raw))


class BuilderService:
    """Builder 연산을 HTTP 전송과 무관하게 제공하는 서비스."""

    def __init__(
        self,
        *,
        output_root: Path,
        client_factory: Callable[..., SourceClient],
        query_service: QueryService | None = None,
        credential_repository: CredentialRepository | None = None,
        provider_test_operation: ProviderTestOperation = default_provider_test,
        provider_test_timeout: float | None = None,
        async_max_workers: int = 10,
        async_max_queue_size: int = 10,
    ) -> None:
        self._output_root = output_root
        self._client_factory = client_factory
        self._build_index = BuildIndex(output_root)  # #309, ADR 0003
        self._query_service = query_service or QueryService()
        repository = credential_repository or _credential_repository_from_env(output_root)
        self._credential_resolver = CredentialResolver(repository)
        self._provider_test_operation = provider_test_operation
        configured_timeout = os.environ.get(_PROVIDER_TEST_TIMEOUT_ENV)
        self._provider_test_timeout = (
            provider_test_timeout
            if provider_test_timeout is not None
            else float(configured_timeout or _DEFAULT_PROVIDER_TEST_TIMEOUT)
        )
        if self._provider_test_timeout <= 0:
            raise ValueError("provider test timeout must be positive")
        self._async_builds = AsyncBuildExecutor(
            max_workers=async_max_workers,
            max_queue_size=async_max_queue_size,
        )

    def _create_client(
        self,
        principal: Principal | None = None,
        *,
        providers: Iterable[str] = (),
        timeout: float | None = None,
        resolved_provider_keys: Mapping[str, str] | None = None,
    ) -> SourceClient:
        """요청 principal의 credential로 격리된 새 provider client를 만든다."""
        provider_names = tuple(dict.fromkeys(providers))
        provider_keys = dict(resolved_provider_keys or {})
        if resolved_provider_keys is None and principal is not None and provider_names:
            provider_keys = self._credential_resolver.provider_keys(
                principal.owner_id, provider_names
            )

        kwargs: dict[str, object] = {}
        if provider_keys:
            if not _factory_accepts_keyword(self._client_factory, "provider_keys"):
                raise RuntimeError("client_factory cannot accept principal provider credentials")
            kwargs["provider_keys"] = provider_keys
            if not _factory_accepts_keyword(self._client_factory, "cache"):
                raise RuntimeError("client_factory cannot disable credential response cache")
            # kpubdata#263이 해결되기 전에는 credential이 cache key에 포함되지 않는다.
            # 사용자별 credential을 쓰는 service client는 환경설정과 무관하게 cache를 끈다.
            kwargs["cache"] = False
        if timeout is not None and _factory_accepts_keyword(self._client_factory, "timeout"):
            kwargs["timeout"] = timeout
        return self._client_factory(**kwargs)

    def _runtime_providers(self) -> tuple[ProviderDescriptor, ...] | ServiceResponse:
        client = self._create_client()
        try:
            return provider_descriptors(client)
        except Exception:
            return ServiceResponse(502, {"error": "provider catalog unavailable"})
        finally:
            _close_request_client(client)

    def _known_provider(self, provider: str) -> ProviderDescriptor | ServiceResponse:
        providers = self._runtime_providers()
        if isinstance(providers, ServiceResponse):
            return providers
        match = next((item for item in providers if item.name == provider), None)
        if match is None:
            return ServiceResponse(404, {"error": "provider not found"})
        return match

    def providers(self, *, principal: Principal) -> ServiceResponse:
        """런타임 Provider 목록과 현재 principal의 configured 상태를 반환한다."""
        descriptors = self._runtime_providers()
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
        descriptor = self._known_provider(provider)
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
                operation=lambda _client, _provider: (_ for _ in ()).throw(RuntimeError()),
            )
            return ServiceResponse(200, cast(dict[str, JsonValue], test_result_body(result)))
        finally:
            if client is not None:
                _close_request_client(client)

    def provider_credential(self, provider: str, *, principal: Principal) -> ServiceResponse:
        """원문 없이 현재 principal의 저장 credential 메타데이터를 반환한다."""
        known = self._known_provider(provider)
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
        known = self._known_provider(provider)
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
        known = self._known_provider(provider)
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

    def query(
        self, body: Mapping[str, JsonValue] | None, *, principal: Principal
    ) -> ServiceResponse:
        """서버가 resolve한 stage 테이블에 대해 검증된 SQL 쿼리 1건을 실행한다."""
        try:
            request = _query_request_from_body(body)
            context = resolve_query_context(self._output_root, request, principal)
            validated = validate_read_only_sql(request.sql)
            result = self._query_service.execute(
                context.table_path, validated.canonical_sql, limit=request.limit
            )
        except PermissionError:
            return ServiceResponse(403, {"error": "forbidden", "code": "forbidden"})
        except QueryArtifactUnavailableError:
            return ServiceResponse(
                404, {"error": "query artifact unavailable", "code": "artifact_unavailable"}
            )
        except QueryContextError as exc:
            return ServiceResponse(400, {"error": str(exc), "code": "invalid_context"})
        except UnsafeQueryError as exc:
            return ServiceResponse(400, {"error": str(exc), "code": "unsafe_query"})
        except QueryBusyError:
            return ServiceResponse(429, {"error": "query is busy", "code": "query_busy"})
        except QueryTimeoutError:
            return ServiceResponse(504, {"error": "query timed out", "code": "query_timeout"})
        except QueryExecutionError:
            return ServiceResponse(
                400, {"error": "query execution failed", "code": "query_execution_failed"}
            )
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc), "code": "invalid_request"})

        return ServiceResponse(
            200,
            {
                "columns": list(result.columns),
                "rows": list(result.rows),
                "truncated": result.truncated,
                "execution_ms": result.execution_ms,
                "startup_ms": result.startup_ms,
                "engine_execution_ms": result.engine_execution_ms,
            },
        )

    def version(self) -> ServiceResponse:
        """Builder API 계약 버전을 반환한다 (#209).

        소비자(Studio 등)가 호출 전에 계약 호환성을 확인할 수 있는 메타 엔드포인트다.
        """
        return ServiceResponse(
            200, {"service": "kpubdata-builder", "api_version": API_CONTRACT_VERSION}
        )

    def catalog(self) -> ServiceResponse:
        """사용 가능한 provider/dataset 카탈로그를 반환한다 (#416, BL2, #436).

        kpubdata Client의 공개 ``datasets.list()`` 로 모든 데이터셋을 조회한 뒤
        provider별로 그룹화한다 (ADR 0011 — Builder가 provider 목록을
        하드코딩하지 않는다). 이전에는 ``getattr(client, "_catalog")`` private
        접근 + 8개 provider 하드코딩 튜플을 써서 kpubdata에 provider가 추가돼도
        카탈로그에 안 떴다 (#436). 시크릿 값은 노출하지 않고 필요 여부만 표시한다.
        """
        client = self._create_client()
        try:
            all_datasets = cast(Client, client).datasets.list()
            auth_provider_names = _authenticated_provider_names(client)
        except Exception as exc:
            return ServiceResponse(502, {"error": f"catalog unavailable: {exc}"})
        finally:
            _close_request_client(client)

        # provider별 그룹화 (등록 순서 보존 위해 dict 사용).
        grouped: dict[str, list[DatasetRef]] = {}
        for ds in all_datasets:
            grouped.setdefault(ds.provider, []).append(ds)

        providers_data: list[JsonValue] = [
            {
                "name": provider_name,
                "datasets": [
                    {
                        "name": item.dataset_key,
                        "title": item.name,
                        "requires_service_key": _requires_service_key(item, auth_provider_names),
                    }
                    for item in items
                ],
            }
            for provider_name, items in grouped.items()
        ]
        return ServiceResponse(200, {"providers": providers_data})

    def validate(self, spec_yaml: str) -> ServiceResponse:
        """BuildSpec을 파싱·검증한다."""
        try:
            spec = _parse_spec_text(spec_yaml)
            validate_spec(spec)
        except SpecLoadError as exc:
            return ServiceResponse(400, {"status": "error", "error": str(exc)})
        except ValidationError as exc:
            body: dict[str, JsonValue] = {"status": "invalid", "problems": list(exc.problems)}
            if exc.structured_problems:
                body["structured_problems"] = [
                    {"code": p.code, "path": p.path, "message": p.message, "hint": p.hint}
                    for p in exc.structured_problems
                ]
            return ServiceResponse(400, body)
        return ServiceResponse(
            200,
            {
                "status": "valid",
                "dataset_id": spec.dataset_id,
                "api_version": API_CONTRACT_VERSION,
            },
        )

    def preview(
        self,
        spec_yaml: str,
        *,
        limit: int = DEFAULT_PREVIEW_LIMIT,
        principal: Principal | None = None,
    ) -> ServiceResponse:
        """각 소스의 스키마와 샘플 행을 산출한다 (파일 미기록)."""
        if limit < 1:
            return ServiceResponse(400, {"error": "'limit' must be a positive integer"})
        spec_or_error = self._load_validated(spec_yaml)
        if isinstance(spec_or_error, ServiceResponse):
            return spec_or_error

        provider_names = tuple(source.provider for source in spec_or_error.sources)
        try:
            provider_keys = (
                self._credential_resolver.provider_keys(principal.owner_id, provider_names)
                if principal is not None
                else {}
            )
            client = self._create_client(
                principal,
                providers=provider_names,
                resolved_provider_keys=provider_keys,
            )
        except (ProviderCredentialConflictError, ValueError) as exc:
            return ServiceResponse(400, {"error": str(exc)})
        except Exception:
            return ServiceResponse(502, {"error": "provider client unavailable"})
        try:
            result = preview_build(spec_or_error, client=client, limit=limit)
        finally:
            _close_request_client(client)
        previews: list[JsonValue] = [
            {
                "source_key": p.source_key,
                "status": p.status,
                "error": _redact_secret_text(p.error, provider_keys.values()),
                "schema": [
                    {
                        "name": column.name,
                        "dtype": column.dtype,
                        "nullable": column.nullable,
                        "unique_count": column.unique_count,
                    }
                    for column in p.schema.columns
                ],
                "sample": list(p.preview.rows),
                "total_rows": p.preview.total_rows,
                "statistics": {
                    "row_count": p.statistics.row_count,
                    "null_counts": dict(p.statistics.null_counts),
                    "duplicate_rate": p.statistics.duplicate_rate,
                },
                "quality_results": cast(
                    JsonValue, [_quality_result_to_json(r) for r in p.quality_results]
                ),
            }
            for p in result.previews
        ]
        return ServiceResponse(200, {"dataset_id": spec_or_error.dataset_id, "previews": previews})

    def build(
        self,
        spec_yaml: str,
        *,
        run_id: str | None = None,
        created_by: str | None = None,
        owner_id: str | None = None,
        principal: Principal | None = None,
    ) -> ServiceResponse:
        """파이프라인을 실행하고 결과를 반환한다.

        응답 코드 정책:
            - 모든 소스 성공: 200
            - 하나라도 소스 fetch/stage가 실패: 502 (upstream 소스 의존 실패).
              매니페스트는 partial 정책으로 남기 때문에 body에 outcomes와 manifest가
              실린다.

        ``owner_id``는 canonical stable owner identity다(#505). ``created_by``는
        기존(#388) display/legacy 라벨로 계속 함께 기록된다 — wire 계약은 바뀌지
        않는다.
        """
        spec_or_error = self._load_validated(spec_yaml)
        if isinstance(spec_or_error, ServiceResponse):
            return spec_or_error

        provider_names = tuple(source.provider for source in spec_or_error.sources)
        try:
            provider_keys = (
                self._credential_resolver.provider_keys(principal.owner_id, provider_names)
                if principal is not None
                else {}
            )
            client = self._create_client(
                principal,
                providers=provider_names,
                resolved_provider_keys=provider_keys,
            )
        except (ProviderCredentialConflictError, ValueError) as exc:
            return ServiceResponse(400, {"error": str(exc)})
        except Exception:
            return ServiceResponse(502, {"error": "provider client unavailable"})
        try:
            result = run_build(
                spec_or_error,
                client=client,
                output_root=self._output_root,
                run_id=run_id,
                created_by=created_by,
                owner_id=owner_id,
            )
        finally:
            _close_request_client(client)
        secret_values = tuple(provider_keys.values())
        if secret_values:
            try:
                manifest_data = json.loads(result.manifest_path.read_text(encoding="utf-8"))
                redacted_manifest = _redact_json_secrets(manifest_data, secret_values)
                if redacted_manifest != manifest_data:
                    result.manifest_path.write_text(
                        json.dumps(redacted_manifest, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
            except (OSError, json.JSONDecodeError):
                return ServiceResponse(500, {"error": "failed to secure build manifest"})
        outcomes: list[JsonValue] = [
            {
                "source_key": outcome.source_key,
                "status": outcome.status,
                "stages_completed": list(outcome.stages_completed),
                "error": _redact_secret_text(outcome.error, secret_values),
            }
            for outcome in result.outcomes
        ]
        status_code = 200 if result.status == "ok" else 502
        body: dict[str, JsonValue] = {
            "status": result.status,
            "run_id": result.context.run_id,
            "outcomes": outcomes,
            "manifest": str(result.manifest_path),
            "api_version": API_CONTRACT_VERSION,
        }
        # 실패한 빌드는 첫 번째 실패 outcome의 error를 최상위 `error` 요약으로 노출해,
        # Studio 같은 소비자가 outcomes 배열을 파싱하지 않고도 사람이 읽을 수 있는
        # 사유를 즉시 표면화할 수 있게 한다 (#226).
        if result.status != "ok":
            first_error = next(
                (o.error for o in result.outcomes if o.status != "ok" and o.error), None
            )
            body["error"] = _redact_secret_text(first_error, secret_values) or "build failed"

        # ADR 0003: 빌드 완료 후 인덱스 갱신 (best-effort, 실패해도 빌드 성공 유지)
        try:
            manifest_data = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            started_at = manifest_data.get("started_at")
            finished_at = manifest_data.get("finished_at")
            self._build_index.insert_or_replace(
                run_id=result.context.run_id,
                status=result.status,  # type: ignore[arg-type]
                started_at=started_at,
                finished_at=finished_at,
                spec_digest=result.spec_digest,
                created_by=manifest_data.get("created_by"),
                owner_id=manifest_data.get("owner_id"),
                # dataset_id는 이 run이 실제로 실행한 canonical spec에서 곧바로 얻는다
                # (snapshot과 동일한 값) — 파생 검색값일 뿐이므로 별도로 추측하지 않는다 (#488).
                dataset_id=spec_or_error.dataset_id,
            )
        except Exception:
            # 인덱스 갱신 실패는 무시 (ADR 0003)
            pass

        return ServiceResponse(status_code, body)

    def submit_build(
        self, spec_yaml: str, *, run_id: str | None = None, created_by: str | None = None
    ) -> ServiceResponse:
        """비동기 build job을 큐에 넣고 초기 상태를 반환한다 (#482)."""
        resolved_run_id = run_id or generate_run_id()
        if self._build_index.get(resolved_run_id) is not None:
            return ServiceResponse(
                409,
                {
                    "error": "run_id already completed",
                    "run_id": resolved_run_id,
                },
            )
        result = self._async_builds.submit(
            spec_yaml=spec_yaml,
            run_id=resolved_run_id,
            created_by=created_by,
            runner=self._run_build_job,
        )
        match result.status:
            case "accepted":
                if result.snapshot is None:
                    raise RuntimeError("accepted async build is missing snapshot")
                return ServiceResponse(202, result.snapshot.to_body())
            case "existing":
                if result.snapshot is None:
                    raise RuntimeError("existing async build is missing snapshot")
                return ServiceResponse(200, result.snapshot.to_body())
            case "queue_full":
                return ServiceResponse(429, {"error": "async build queue is full"})
            case unreachable:
                assert_never(unreachable)

    def build_status(self, run_id: str) -> ServiceResponse:
        """active/terminal 비동기 build job 상태를 반환한다 (#482)."""
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})
        snapshot = self._async_builds.get(run_id)
        if snapshot is None:
            return ServiceResponse(404, {"error": f"build job not found: {run_id}"})
        return ServiceResponse(200, snapshot.to_body())

    def _run_build_job(
        self, spec_yaml: str, run_id: str, created_by: str | None
    ) -> ServiceResponse:
        return self.build(spec_yaml, run_id=run_id, created_by=created_by)

    def artifacts(self, run_id: str) -> ServiceResponse:
        """실행 워크스페이스의 산출물 파일 목록을 반환한다."""
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})

        run_dir = self._output_root / run_id
        ensure_within(self._output_root, run_dir, label="run directory")
        if not run_dir.exists():
            return ServiceResponse(404, {"error": f"run not found: {run_id}"})

        files = sorted(
            str(path.relative_to(run_dir)) for path in run_dir.rglob("*") if path.is_file()
        )
        return ServiceResponse(200, {"run_id": run_id, "files": list(files)})

    def manifest(self, run_id: str) -> ServiceResponse:
        """persisted manifest를 읽되 내부 ownership 필드는 wire에서 제거한다.

        ``owner_id``는 디스크의 ``manifest.json``과 BuildIndex에만 저장되는 내부
        식별자다(#505). OpenAPI ``BuildManifest``는 실제 HTTP 응답의 SSOT이므로
        이 메서드에서 명시적으로 제거해 공개 API 필드가 되지 않게 한다.
        """
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})

        run_dir = self._output_root / run_id
        ensure_within(self._output_root, run_dir, label="run directory")
        manifest_path = run_dir / "manifest.json"
        ensure_within(run_dir, manifest_path, label="manifest file")
        if not manifest_path.exists():
            return ServiceResponse(404, {"error": f"manifest not found: {run_id}"})

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return ServiceResponse(500, {"error": f"invalid manifest JSON: {exc.msg}"})
        except OSError as exc:
            return ServiceResponse(500, {"error": f"failed to read manifest: {exc}"})
        if not isinstance(manifest, dict):
            return ServiceResponse(500, {"error": "invalid manifest: expected object"})
        manifest.pop("owner_id", None)
        return ServiceResponse(200, cast(dict[str, JsonValue], manifest))

    def spec(self, run_id: str) -> ServiceResponse:
        """run에서 실제 사용한 canonical BuildSpec snapshot과 digest를 반환한다."""
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})

        run_dir = self._output_root / run_id
        ensure_within(self._output_root, run_dir, label="run directory")
        if not run_dir.is_dir():
            return ServiceResponse(404, {"error": f"run not found: {run_id}"})

        snapshot_path = run_dir / BUILDSPEC_SNAPSHOT_FILENAME
        ensure_within(run_dir, snapshot_path, label="BuildSpec snapshot")
        if not snapshot_path.is_file():
            return ServiceResponse(
                404, {"error": f"BuildSpec snapshot unavailable for run: {run_id}"}
            )
        try:
            payload = snapshot_path.read_bytes()
            spec_text = payload.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return ServiceResponse(500, {"error": f"failed to read BuildSpec snapshot: {exc}"})
        return ServiceResponse(
            200,
            {
                "run_id": run_id,
                "spec": spec_text,
                "spec_digest": compute_spec_digest(payload),
            },
        )

    def serve_artifact_file(self, run_id: str, file_path: str) -> ServiceResponse | FileResponse:
        """실행 워크스페이스의 특정 파일을 제공한다 (#323).

        경로 트래버설 공격을 방지하기 위해 run_id와 file_path 모두
        검증하며, 심볼릭 링크를 따르지 않는다.

        매개변수:
            run_id: 실행 식별자.
            file_path: 요청된 파일 경로 (run_id 하위의 상대 경로).

        반환값:
            FileResponse (파일 발견 시) 또는 ServiceResponse (오류 시).
        """
        # run_id 검증
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})

        # run_dir 확인
        run_dir = self._output_root / run_id
        ensure_within(self._output_root, run_dir, label="run directory")
        if not run_dir.exists():
            return ServiceResponse(404, {"error": f"run not found: {run_id}"})

        # file_path 검증 (경로 트래버설 방지)
        try:
            validate_path_segment(file_path, field_name="file_path")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})

        # 요청된 파일의 전체 경로 계산
        requested_file = run_dir / file_path
        # 경로가 run_dir 내에 있는지 확인 (심볼릭 링크도 해석하여 안전 검사)
        ensure_within(run_dir, requested_file, label="artifact file")

        if not requested_file.exists():
            return ServiceResponse(404, {"error": f"file not found: {file_path}"})
        if not requested_file.is_file():
            return ServiceResponse(400, {"error": f"not a file: {file_path}"})

        # 파일명 추출 (Content-Disposition용)
        filename = requested_file.name

        return FileResponse(status_code=200, file_path=requested_file, filename=filename)

    def list_builds(
        self, *, limit: int = 50, principal: Principal | None = None
    ) -> ServiceResponse:
        """실행 이력 목록을 최신 완료 시각 기준 내림차순 반환한다.

        ADR 0003에 따라 SQLite 인덱스를 우선 조회하고, 인덱스가 없거나
        비어있으면 파일시스템 스캔으로 폴백한다. ENFORCE_OWNERSHIP+oidc일 때는
        두 경로 모두 _apply_ownership으로 본인 소유 run만 노출한다 (#433).
        """
        # 인덱스 우선 조회
        try:
            entries = self._build_index.list_builds(limit=limit)
            if entries:
                index_builds: list[_BuildListEntry] = [
                    {
                        "run_id": entry.run_id,
                        "status": entry.status,
                        "started_at": entry.started_at,
                        "finished_at": entry.finished_at,
                        "created_by": entry.created_by,
                        "owner_id": entry.owner_id,
                    }
                    for entry in entries
                ]
                filtered = _strip_internal_fields(_apply_ownership(index_builds, principal))
                return ServiceResponse(200, {"builds": cast(list[JsonValue], filtered)})
        except Exception:
            # 인덱스 조회 실패. ENFORCE_OWNERSHIP+oidc면 타인 run이 폴백으로
            # 새어나갈 수 있으므로 fail-closed로 빈 배열을 반환한다 (#433).
            # 일반 모드는 기존대로 파일시스템 폴백으로 진행한다 (ADR 0003).
            if _enforce_ownership() and principal and principal.kind == "oidc":
                logger.warning(
                    "build index query failed; returning empty list "
                    "(ownership enforced, fail-closed)",
                    exc_info=True,
                )
                return ServiceResponse(200, {"builds": []})

        # 폴백: 파일시스템 스캔
        if not self._output_root.exists():
            return ServiceResponse(200, {"builds": []})

        candidates = heapq.nlargest(
            limit,
            (d for d in self._output_root.iterdir() if d.is_dir()),
            key=lambda p: p.stat().st_mtime,
        )
        fs_builds: list[_BuildListEntry] = []
        for run_dir in candidates:
            manifest_path = run_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            fs_builds.append(
                {
                    "run_id": run_dir.name,
                    "status": "failed" if manifest.get("errors") else "ok",
                    "started_at": manifest.get("started_at"),
                    "finished_at": manifest.get("finished_at"),
                    "created_by": manifest.get("created_by"),
                    "owner_id": manifest.get("owner_id"),
                }
            )
        filtered = _strip_internal_fields(_apply_ownership(fs_builds, principal))
        return ServiceResponse(200, {"builds": cast(list[JsonValue], filtered)})

    def _dataset_records(self, principal: Principal | None) -> list[datasets_service.RunRecord]:
        """dataset_id가 있는 접근 가능한 모든 run을 얻는다 (인덱스 우선, 파일시스템 폴백).

        ownership 필터를 grouping/latest 선정보다 먼저 적용한다 — 동일 dataset_id의
        타 사용자 run이 latest 후보에 섞이지 않게 한다 (#488 semantics D).
        """
        index_records = datasets_service.collect_run_records_from_index(self._build_index) or []
        filesystem_records = datasets_service.collect_run_records_from_filesystem(self._output_root)
        records = datasets_service.merge_run_records(index_records, filesystem_records)
        records = datasets_service.retain_canonical_run_records(self._output_root, records)
        return datasets_service.filter_ownership(records, principal, enforce=_enforce_ownership())

    def _dataset_records_for(
        self, dataset_id: str, principal: Principal | None
    ) -> list[datasets_service.RunRecord]:
        """특정 dataset_id의 접근 가능한 canonical run을 모두 얻는다."""
        return [
            record for record in self._dataset_records(principal) if record.dataset_id == dataset_id
        ]

    def list_datasets(
        self, *, limit: int = 50, principal: Principal | None = None
    ) -> ServiceResponse:
        """동일 dataset_id의 여러 run을 하나의 built dataset으로 묶어 목록을 반환한다 (#488).

        각 dataset은 접근 가능한 run 중 latest run(finished_at 기준, 동일 시각은
        run_id 내림차순 타이브레이크)의 canonical snapshot·manifest·stage 상태로
        요약된다. legacy run(snapshot 없음)은 grouping 대상에서 제외된다.
        """
        records = self._dataset_records(principal)
        latest_by_dataset = datasets_service.group_latest_by_dataset(records)
        ordered = sorted(
            latest_by_dataset.values(),
            key=datasets_service.sort_key,
            reverse=True,
        )
        items: list[JsonValue] = []
        for record in ordered:
            if len(items) >= limit:
                break
            view = datasets_service.build_dataset_summary(self._output_root, record)
            if view is not None:
                items.append(view)
        return ServiceResponse(200, {"datasets": items})

    def get_dataset(
        self, dataset_id: str, *, principal: Principal | None = None
    ) -> ServiceResponse:
        """단일 built dataset의 canonical 요약을 반환한다 (#488).

        접근 가능한 run이 하나도 없으면(dataset_id가 실제로 없거나, 있어도 전부
        타 사용자 소유이면) 404 — 어느 경우인지는 구분해 노출하지 않는다.
        """
        records = self._dataset_records_for(dataset_id, principal)
        if not records:
            return ServiceResponse(404, {"error": f"dataset not found: {dataset_id}"})
        latest = datasets_service.pick_latest(records)
        view = datasets_service.build_dataset_summary(self._output_root, latest)
        if view is None:
            return ServiceResponse(404, {"error": f"dataset not found: {dataset_id}"})
        view["run_count"] = len(records)
        return ServiceResponse(200, view)

    def list_dataset_runs(
        self, dataset_id: str, *, limit: int = 50, principal: Principal | None = None
    ) -> ServiceResponse:
        """dataset_id의 접근 가능한 run history를 최신순으로 반환한다 (#488).

        타 사용자의 run은 제외된다. 접근 가능한 run이 하나도 없으면 404 —
        /datasets/{dataset_id}와 동일한 존재 판정 정책을 공유한다.
        """
        records = self._dataset_records_for(dataset_id, principal)
        if not records:
            return ServiceResponse(404, {"error": f"dataset not found: {dataset_id}"})
        ordered = sorted(records, key=datasets_service.sort_key, reverse=True)[:limit]
        runs: list[JsonValue] = [
            {
                "run_id": r.run_id,
                "status": r.status,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "spec_digest": r.spec_digest,
                "created_by": r.created_by,
            }
            for r in ordered
        ]
        return ServiceResponse(200, {"dataset_id": dataset_id, "runs": runs})

    def get_dataset_quality_history(
        self, dataset_id: str, *, limit: int = 30, principal: Principal | None = None
    ) -> ServiceResponse:
        """dataset_id의 접근 가능한 run들에 대한 quality PASS/WARN/FAIL 집계 이력 (#486).

        dataset→run 조회는 #488의 ``_dataset_records_for``(ownership 필터링 포함)를
        그대로 재사용한다 — 새 dataset grouping/index를 만들지 않는다. 존재/ownership
        판정은 ``/datasets/{dataset_id}``, ``/datasets/{dataset_id}/runs``와 동일하다.
        """
        records = self._dataset_records_for(dataset_id, principal)
        if not records:
            return ServiceResponse(404, {"error": f"dataset not found: {dataset_id}"})
        ordered = sorted(records, key=datasets_service.sort_key, reverse=True)[:limit]
        runs: list[JsonValue] = []
        for r in ordered:
            manifest = datasets_service.read_manifest(self._output_root, r.run_id) or {}
            runs.append(cast(JsonValue, quality_service.summarize_run_quality(r, manifest)))
        return ServiceResponse(200, {"dataset_id": dataset_id, "runs": runs})

    def get_build_quality(self, run_id: str) -> ServiceResponse:
        """run의 구조화된 Quality 결과와 schema drift를 조회한다 (#486, #514).

        manifest.json에 이미 저장된 source_key별 quality_results/schema_drift를
        그대로 노출한다 — 별도 계산을 다시 하지 않는다(정본은 manifest).
        `availability`/`evaluated_checks`는 빈 매핑이 "평가했지만 0건"인지
        "애초에 계산된 적이 없음"(legacy/partial run)인지 구분한다(#514).
        """
        manifest = datasets_service.read_manifest(self._output_root, run_id)
        if manifest is None:
            return ServiceResponse(404, {"error": f"manifest not found: {run_id}"})
        known_sources = stages_service.known_source_keys(manifest)
        availability, evaluated_checks = quality_service.quality_availability(
            manifest, known_sources
        )
        quality_results = manifest.get("quality_results")
        schema_drift = manifest.get("schema_drift")
        return ServiceResponse(
            200,
            {
                "run_id": run_id,
                "availability": availability,
                "evaluated_checks": evaluated_checks,
                "quality_results": cast(
                    JsonValue, quality_results if isinstance(quality_results, dict) else {}
                ),
                "schema_drift": cast(
                    JsonValue, schema_drift if isinstance(schema_drift, dict) else {}
                ),
            },
        )

    def list_run_stages(self, run_id: str) -> ServiceResponse:
        """run에 알려진 모든 source의 Bronze/Silver/Gold 상태를 반환한다 (#488).

        호출 전에 run_id 검증·존재 확인·ownership 게이팅이 끝나 있어야 한다
        (dispatch가 다른 /builds/{run_id}/* 라우트와 동일한 순서로 처리한다).
        """
        manifest = datasets_service.read_manifest(self._output_root, run_id)
        if manifest is None:
            return ServiceResponse(404, {"error": f"manifest not found: {run_id}"})
        summaries = stages_service.list_run_stages(self._output_root, run_id, manifest)
        sources: list[JsonValue] = [
            {
                "source_key": s.source_key,
                "bronze": {"status": s.bronze, "available": s.bronze == "completed"},
                "silver": {"status": s.silver, "available": s.silver == "completed"},
                "gold": {"status": s.gold, "available": s.gold == "completed"},
            }
            for s in summaries
        ]
        return ServiceResponse(200, {"run_id": run_id, "sources": sources})

    def get_run_stage_detail(
        self, run_id: str, stage: str, source_key: str, *, limit: int
    ) -> ServiceResponse:
        """단일 source의 단일 stage에 대한 안전한 summary/preview를 반환한다 (#488).

        순서: stage 이름 검증(구조) → manifest에서 known source 확인 → 각 stage
        reader가 sidecar만 읽어 응답을 구성한다. raw fetch_params/export
        options/credential/absolute path는 어디에도 담지 않는다.
        """
        if stage not in stages_service.STAGE_NAMES:
            return ServiceResponse(
                400, {"error": f"invalid stage: {stage!r}; must be one of bronze/silver/gold"}
            )
        manifest = datasets_service.read_manifest(self._output_root, run_id)
        if manifest is None:
            return ServiceResponse(404, {"error": f"manifest not found: {run_id}"})
        summary = stages_service.stage_status_for_source(
            self._output_root, run_id, manifest, source_key
        )
        if summary is None:
            return ServiceResponse(404, {"error": f"unknown source: {source_key}"})

        status = stages_service.stage_status_of(summary, stage)
        body: dict[str, JsonValue] = {
            "run_id": run_id,
            "stage": stage,
            "source_key": source_key,
            "status": status,
            "available": status == "completed",
        }

        if stage == "bronze":
            bronze = stages_service.bronze_detail(self._output_root, run_id, source_key)
            spec = datasets_service.read_snapshot_spec(self._output_root, run_id)
            matched = stages_service.match_source_ref(spec, source_key) if spec else None
            body["provider"] = matched.provider if matched is not None else None
            body["dataset"] = matched.dataset if matched is not None else None
            body["fetched_at"] = bronze.fetched_at if bronze is not None else None
            body["record_count"] = bronze.record_count if bronze is not None else None
        elif stage == "silver":
            silver = stages_service.silver_detail(
                self._output_root, run_id, source_key, limit=limit
            )
            body["row_count"] = silver.row_count if silver is not None else None
            body["schema"] = silver.schema if silver is not None else []
            body["statistics"] = silver.statistics if silver is not None else None
            body["validation"] = silver.validation if silver is not None else None
            body["sample"] = silver.sample if silver is not None else []
        else:  # gold
            gold = stages_service.gold_detail(self._output_root, run_id, source_key)
            body["row_count"] = gold.row_count if gold is not None else None
            body["columns"] = cast(JsonValue, gold.columns) if gold is not None else []
            body["splits"] = cast(JsonValue, gold.splits) if gold is not None else None
            body["exports"] = (
                cast(JsonValue, [{"kind": kind} for kind in gold.export_kinds])
                if gold is not None
                else []
            )
            # Gold sample sidecar가 아직 없으므로 만들어내지 않는다 — Silver sample을
            # 가장하지 않고 명시적으로 unavailable을 표현한다.
            body["sample"] = None
            body["sample_available"] = False

        return ServiceResponse(200, body)

    def _load_validated(self, spec_yaml: str) -> BuildSpec | ServiceResponse:
        """spec_yaml을 파싱·검증하고, 실패 시 오류 ServiceResponse를 반환한다."""
        try:
            spec = _parse_spec_text(spec_yaml)
            validate_spec(spec)
        except SpecLoadError as exc:
            return ServiceResponse(400, {"status": "error", "error": str(exc)})
        except ValidationError as exc:
            body: dict[str, JsonValue] = {"status": "invalid", "problems": list(exc.problems)}
            if exc.structured_problems:
                body["structured_problems"] = [
                    {"code": p.code, "path": p.path, "message": p.message, "hint": p.hint}
                    for p in exc.structured_problems
                ]
            return ServiceResponse(400, body)
        return spec


def _query_request_from_body(body: Mapping[str, JsonValue] | None) -> QueryRequest:
    if body is None:
        raise ValueError("request body is required")
    if not set(body).issubset({"dataset_id", "run_id", "stage", "source", "sql", "limit"}):
        raise ValueError("request contains unknown fields")
    dataset_id = body.get("dataset_id")
    run_id = body.get("run_id")
    stage = body.get("stage")
    sql = body.get("sql")
    source = body.get("source")
    limit = body.get("limit", 100)
    if not isinstance(dataset_id, str) or not dataset_id:
        raise ValueError("dataset_id must be a non-empty string")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    if stage not in ("silver", "gold"):
        raise ValueError("stage must be silver or gold")
    if not isinstance(sql, str) or not sql:
        raise ValueError("sql must be a non-empty string")
    if source is not None and (not isinstance(source, str) or not source):
        raise ValueError("source must be a non-empty string when provided")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer from 1 to 500")
    return QueryRequest(
        dataset_id=dataset_id,
        run_id=run_id,
        stage=cast(QueryStage, stage),
        source=source,
        sql=sql,
        limit=limit,
    )


def dispatch(
    service: BuilderService,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
    query: str = "",
    *,
    api_key: str | None = None,
    bearer_token: str | None = None,
) -> ServiceResponse | FileResponse:
    """(method, path)를 BuilderService 연산으로 라우팅한다.

    GET /healthz는 인증 없이 반환하고 (#372), 그 외 엔드포인트는
    authenticate()로 Principal을 얻어 인증 게이트를 통과한 후 라우팅한다.
    dev-mode이면 인증 생략, 그 외는 fail-closed(401)로 동작한다 (#248, #384).

    반환값:
        ServiceResponse 또는 FileResponse (#323).
    """
    # /healthz는 인증 게이트 밖에서 무인증 노출 (#372).
    if method == "GET" and path == "/healthz":
        return ServiceResponse(200, {"status": "ok"})

    # 인증 게이트 (#384): Principal을 얻지 못하면 401.
    principal = authenticate(api_key=api_key, bearer_token=bearer_token)
    if isinstance(principal, AuthError):
        return ServiceResponse(principal.status_code, {"error": principal.reason})

    for adapter in ROUTE_ADAPTERS:
        response = adapter(service, method, path, body, query, principal)
        if response is not None:
            return response

    return ServiceResponse(404, {"error": f"not found: {method} {path}"})


__all__ = ["API_CONTRACT_VERSION", "BuilderService", "ServiceResponse", "FileResponse", "dispatch"]
