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
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from datetime import datetime, timezone
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
from ..events import BuildEvent, BuildEventStore
from ..ingestion import IngestionError, parse_tabular_bytes
from ..manifest import status_from_manifest
from ..pipeline import (
    DEFAULT_PREVIEW_SEED,
    CancellationProbe,
    SampleMode,
    preview_build,
    run_build,
)
from ..publishers import PUBLISHER_REGISTRY
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
from ..spec.models import SOURCE_FILE_FORMATS
from ..spec.serializer import BUILDSPEC_SNAPSHOT_FILENAME, compute_spec_digest
from ..spec.validator import validate_spec
from ..stages._path_safety import ensure_within, validate_path_segment
from ..stages.bronze.build import SourceClient
from ..store import BuildIndex
from ..tabular import DEFAULT_PREVIEW_LIMIT
from ..uploads import (
    SQLiteUploadRepository,
    UploadMetadata,
    UploadRepository,
    resolve_max_upload_bytes,
)
from . import datasets as datasets_service
from . import events as events_service
from . import monitoring as monitoring_service
from . import ownership as ownership_module
from . import publish as publish_service
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
from .routes import uploads as uploads_route
from .routes.core import MAX_PREVIEW_LIMIT

logger = logging.getLogger(__name__)

_CREDENTIAL_MASTER_KEY_ENV = "KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY"
_PROVIDER_TEST_TIMEOUT_ENV = "KPUBDATA_BUILDER_PROVIDER_TEST_TIMEOUT"
_DEFAULT_PROVIDER_TEST_TIMEOUT = 10.0


# /preview의 limit 방어적 상한 (#497). 기존에는 상한이 없었다 — Preview는 전체
@runtime_checkable
class _CloseableClient(Protocol):
    def close(self) -> None: ...


def _close_request_client(client: SourceClient) -> None:
    if isinstance(client, _CloseableClient):
        client.close()


def _raise_provider_test_error(client: SourceClient, provider: str) -> None:
    """provider_status의 client 생성 실패 fallback에서 쓰는 항상-raise operation."""
    raise RuntimeError()


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


def _catalog_dataset_body(dataset: DatasetRef, requires_service_key: bool) -> dict[str, JsonValue]:
    """DatasetRef의 public/canonical metadata만 allowlist로 직렬화한다 (#490).

    ``raw_metadata``는 provider 내부 정보와 secret-like 값이 섞일 수 있어
    절대 그대로 노출하지 않는다 — UI 탐색에 필요한 필드만 명시적으로 골라
    담는다. metadata가 없는 dataset은 description/source_url/query_support가
    null, tags/operations가 빈 배열로 직렬화된다(응답 전체를 깨지 않는다).
    """
    query_support: JsonValue = None
    if dataset.query_support is not None:
        query_support = {
            "pagination": dataset.query_support.pagination.value,
            "filterable_fields": cast(JsonValue, sorted(dataset.query_support.filterable_fields)),
            "sortable_fields": cast(JsonValue, sorted(dataset.query_support.sortable_fields)),
            "time_range": dataset.query_support.time_range,
            "max_page_size": dataset.query_support.max_page_size,
        }
    return {
        "name": dataset.dataset_key,
        "title": dataset.name,
        "description": dataset.description,
        "tags": cast(JsonValue, sorted(dataset.tags)),
        "source_url": dataset.source_url,
        "representation": dataset.representation.value,
        "operations": cast(JsonValue, sorted(op.value for op in dataset.operations)),
        "query_support": query_support,
        "requires_service_key": requires_service_key,
    }


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
# 1.9.0 -> 1.10.0: POST /preview에 Source↔Silver diff와 sample_mode(first/random)
# 옵션 추가 (#497, additive — 기존 필드는 유지된다). limit 상한(1000) 신규 도입은
# behavioral tightening(이전엔 상한 없음) — 그 이상 값은 400.
# 1.10.0 -> 1.11.0: GET /monitoring/summary, GET /monitoring/builds 추가 (#516,
# additive — 기존 엔드포인트는 변경되지 않는다). Async build queue/worker는
# 기존 AsyncBuildExecutor/AsyncBuildJobRegistry(#511/#513)의 read-only
# snapshot을 반영하며, 정상 runtime에서는 availability=available이다.
# MonitoringSummaryResponse.status(healthy/degraded)는 required subsystem
# availability로부터 계산되는 deterministic aggregate다(latency threshold
# 미사용).
# 1.11.0 -> 1.12.0: BuildSpec.composition(JoinSpec)과 POST /build 응답의
# composition 키, manifest.composition(CompositionProvenance) 추가 (#506,
# additive — 기존 필드/엔드포인트는 변경되지 않는다).
# 1.12.0 -> 1.13.0: BuildSpec sources[]에 kind="file"/"url"을 추가하고 (기존
# provider/dataset source는 kind="public_api"로 additive 해석), POST /uploads,
# GET /uploads/{upload_id}, DELETE /uploads/{upload_id}를 추가한다(#498,
# additive — 기존 엔드포인트/kind 없는 source는 변경되지 않는다). url source는
# P0 범위(GET, Auth=None, https만 허용)로 SSRF를 방어하는 safe fetch를 거친다.
# 1.13.0 -> 1.14.0: GET /builds/{run_id}/events를 추가한다(#496, additive —
# 기존 엔드포인트는 변경되지 않는다). run/source fetch/medallion
# stage(bronze/silver/gold/export)/quality checkpoint의 structured event
# append-only timeline을 raw logger 파싱 없이 조회할 수 있다. limit/tail
# query parameter는 bounded(기본 200, 상한 1000)이며 반환은 항상 chronological
# ascending이다. Monitoring(#516)의 시스템 aggregate와는 역할이 분리된다 — 이
# endpoint는 단일 run의 이벤트만 다룬다.
# 1.14.0 -> 1.15.0: /catalog 응답(CatalogDataset)에 탐색용 metadata(description/
# tags/source_url/representation/operations/query_support)를 추가한다(#490,
# additive — 기존 필드는 유지되며 raw_metadata는 노출하지 않는다).
# 1.15.0 -> 1.16.0: async build job 표면을 계약에 문서화한다 — POST /builds(202/200
# idempotent/409/429)와 GET /builds/{run_id}(잡 상태 polling) 추가(#480). 잡 상태
# 조회에 ownership 게이트를 적용해 cross-owner의 build 출력(response) 노출을
# 차단한다(behavioral tightening — 이전에는 미검사).
# 1.16.0 -> 1.17.0: build publish 표면을 추가한다(#491) — GET /builds/{run_id}/publish/
# readiness와 POST /builds/{run_id}/publish(idempotent receipt, TOCTOU 재검사).
# 1.17.0 -> 1.18.0: POST /builds/{run_id}/cancel을 추가한다(#481, ADR 0008 —
# additive). queued job은 실행 전에 즉시 cancelled로, running job은 cancelling을
# 거쳐 안전한 stage 경계에서 cancelled로 종결된다(강제 종료 없음). BuildJobStatus
# 어휘(queued/running/cancelling/succeeded/failed/cancelled)는 그대로 재사용한다.
# 함께 additive로: BuildManifest에 status/partial(부분 산출물 표시),
# BuildEventName에 run_cancelled, BuildSummary.status enum에 cancelled(이미
# BuildIndex/dataset 계약이 쓰던 값이 이제 실제로 관측 가능해진다).
# 1.18.0 -> 1.19.0: publish receipt 운영 경로를 추가한다(#551, additive) —
# GET /builds/{run_id}/publish/receipt(unknown 상태 조회),
# POST /builds/{run_id}/publish/reconcile(원격 상태 확인 후 succeeded 확정 또는
# reset으로 재게시 허용), DELETE /builds/{run_id}/publish/receipt(명시적 reset,
# 감사 로그 기록). reconcile은 원격 판단 불가 시 503로 아무 것도 변경하지 않는다.
API_CONTRACT_VERSION = "1.19.0"


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


def _upload_metadata_body(metadata: UploadMetadata) -> dict[str, JsonValue]:
    """UploadMetadata를 wire JSON으로 변환한다 (#498). content는 절대 포함하지 않는다."""
    return {
        "upload_id": metadata.upload_id,
        "format": metadata.format,
        "encoding": metadata.encoding,
        "size_bytes": metadata.size_bytes,
        "original_filename": metadata.original_filename,
        "created_at": metadata.created_at,
    }


def _parse_spec_text(spec_yaml: str) -> BuildSpec:
    """YAML 텍스트를 BuildSpec으로 파싱한다."""
    raw = cast(object, yaml.safe_load(spec_yaml))
    if not isinstance(raw, dict):
        raise SpecLoadError("top-level YAML must be a mapping")
    return parse_spec(cast(dict[str, object], raw))


def _publish_receipt_response(
    claim_status: publish_service.PublishClaimStatus,
    receipt: publish_service.PublishReceipt,
) -> ServiceResponse | None:
    """기존 receipt 상태를 replay/409 wire response로 변환한다."""
    if claim_status == "claimed":
        return None
    if claim_status == "replay" and receipt.result is not None:
        return ServiceResponse(200, cast(dict[str, JsonValue], receipt.result))
    if claim_status == "replay":
        claim_status = "state_unknown"
    conflict_codes = {
        "in_progress": (
            "publish_in_progress",
            "publish operation is already in progress",
        ),
        "state_unknown": (
            "publish_state_unknown",
            "publish operation outcome is unknown; automatic retry is blocked",
        ),
        "conflict": (
            "publish_conflict",
            "this run and destination were already published with different options",
        ),
    }
    code, message = conflict_codes[claim_status]
    return ServiceResponse(409, {"error": message, "code": code})


class BuilderService:
    """Builder 연산을 HTTP 전송과 무관하게 제공하는 서비스."""

    def __init__(
        self,
        *,
        output_root: Path,
        client_factory: Callable[..., SourceClient],
        query_service: QueryService | None = None,
        credential_repository: CredentialRepository | None = None,
        upload_repository: UploadRepository | None = None,
        provider_test_operation: ProviderTestOperation = default_provider_test,
        provider_test_timeout: float | None = None,
        async_max_workers: int = 10,
        async_max_queue_size: int = 10,
    ) -> None:
        self._output_root = output_root
        self._client_factory = client_factory
        self._build_index = BuildIndex(output_root)  # #309, ADR 0003
        # run event timeline 저장소(#496). `_upload_repository`와 동일하게 지연
        # 생성한다 — preview는 build를 전혀 실행하지 않아 event를 남길 일이
        # 없으므로("Preview writes no files" 기존 계약, #497 범위 밖 #496도
        # 동일), 실제로 처음 쓰일 때(build/submit_build/events 조회)까지
        # `_build_events.sqlite` 흔적을 남기지 않는다.
        self._event_store_lazy: BuildEventStore | None = None
        self._event_store_lock = threading.Lock()
        # kind="file" source(#498)의 업로드 저장소. 명시적으로 주입되지 않으면
        # 실제로 처음 쓰일 때까지 SQLite 파일을 만들지 않는다(지연 생성,
        # `_upload_repository` property) — credential repository(마스터 키
        # 미설정 시 None)처럼, 업로드 기능을 쓰지 않는 워크스페이스에는 흔적을
        # 남기지 않는다(예: preview는 파일을 하나도 쓰지 않는다는 기존 계약).
        self._upload_repository_override: UploadRepository | None = upload_repository
        self._upload_repository_lazy: UploadRepository | None = None
        self._upload_repository_lock = threading.Lock()
        # Monitoring API용 bounded latency recorder (#516). dispatch()가 매 요청
        # 처리 시간을 기록한다 — 인스턴스별로 분리해 테스트 간 상태가 섞이지 않는다.
        self._latency_recorder = monitoring_service.LatencyRecorder()
        # 외부 publish side effect의 durable idempotency receipt. 객체 생성은
        # 파일을 만들지 않으며 최초 POST claim 때 SQLite를 지연 초기화한다.
        self._publish_receipts = publish_service.PublishReceiptStore(output_root)
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
            # running job이 안전 경계에서 실제로 cancelled로 종결되는 순간 정확히
            # 한 번 호출된다 (#481). 종결 event는 job의 terminal 전이를 확정하는
            # 쪽에서만 남겨, queued 취소와 running 취소가 똑같이 run_cancelled
            # 하나로 끝나고 한 run에 종결 event가 둘 생기지 않는다.
            on_cancelled=self._record_run_cancelled,
        )

    @property
    def _event_store(self) -> BuildEventStore:
        """run event timeline 저장소를 지연 생성해 반환한다 (#496).

        처음 접근될 때만 ``_build_events.sqlite``를 만든다 — preview만 하는
        워크스페이스에는 흔적을 남기지 않는다(기존 "Preview writes no files"
        계약과 동일한 이유로 지연 생성한다).
        """
        if self._event_store_lazy is None:
            with self._event_store_lock:
                if self._event_store_lazy is None:
                    self._event_store_lazy = BuildEventStore(self._output_root)
        return self._event_store_lazy

    @property
    def _upload_repository(self) -> UploadRepository:
        """kind="file" source(#498)의 업로드 저장소를 지연 생성해 반환한다.

        명시적으로 주입됐으면 그대로 쓴다. 아니면 처음 호출될 때만
        SQLite 파일을 만든다 — preview/build가 업로드를 전혀 참조하지 않는
        워크스페이스에는 ``.service/uploads.sqlite3`` 흔적을 남기지 않는다.
        """
        if self._upload_repository_override is not None:
            return self._upload_repository_override
        with self._upload_repository_lock:
            if self._upload_repository_lazy is None:
                self._upload_repository_lazy = SQLiteUploadRepository(
                    self._output_root / ".service" / "uploads.sqlite3",
                    max_bytes=resolve_max_upload_bytes(),
                )
            return self._upload_repository_lazy

    def _upload_repository_for(self, spec: BuildSpec) -> UploadRepository | None:
        """spec에 ``kind="file"`` source가 있을 때만 업로드 저장소를 만든다 (#498).

        file source가 없는 preview/build는 이 property를 아예 건드리지 않아
        지연 생성이 실제로 지연되게 한다 — property 자체를 호출하면(설령
        결과를 안 쓰더라도) 매 요청마다 SQLite를 초기화하게 되므로 여기서
        먼저 필요 여부를 가른다.
        """
        if any(source.kind == "file" for source in spec.sources):
            return self._upload_repository
        return None

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
                operation=_raise_provider_test_error,
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

    def create_upload(
        self,
        raw: bytes,
        *,
        format: str,  # noqa: A002 - 계약 필드명과 맞춘다
        encoding: str,
        original_filename: str | None,
        principal: Principal,
    ) -> ServiceResponse:
        """업로드 content를 저장하고 즉시 파싱 가능한지 검증한다 (#498).

        저장은 owner_id로 격리된다 — 나중에 BuildSpec의 ``kind="file"`` source가
        이 upload_id를 참조하려면 같은 principal이어야 한다(``build``/``preview``의
        resolver가 다시 확인한다). 파싱 가능성은 여기서 fail-fast로 확인한다 —
        나중에 build 시점에야 손상된 파일임을 알게 되는 것을 피한다.
        """
        if principal.owner_id is None:
            return ServiceResponse(403, {"error": "stable principal is required"})
        if format not in SOURCE_FILE_FORMATS:
            return ServiceResponse(
                400,
                {"error": f"format must be one of {SOURCE_FILE_FORMATS}, got {format!r}"},
            )
        try:
            _ = parse_tabular_bytes(raw, format=format, encoding=encoding)
        except IngestionError as exc:
            return ServiceResponse(400, {"error": str(exc)})
        try:
            metadata = self._upload_repository.put(
                principal.owner_id,
                content=raw,
                format=format,
                encoding=encoding,
                original_filename=original_filename,
            )
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})
        return ServiceResponse(200, _upload_metadata_body(metadata))

    def get_upload(self, upload_id: str, *, principal: Principal) -> ServiceResponse:
        """현재 principal 소유 업로드의 안전한 메타데이터만 반환한다 (content 제외)."""
        if principal.owner_id is None:
            return ServiceResponse(403, {"error": "stable principal is required"})
        metadata = self._upload_repository.get_metadata(principal.owner_id, upload_id)
        if metadata is None:
            return ServiceResponse(404, {"error": f"upload not found: {upload_id}"})
        return ServiceResponse(200, _upload_metadata_body(metadata))

    def delete_upload(self, upload_id: str, *, principal: Principal) -> ServiceResponse:
        """현재 principal 소유 업로드만 삭제한다."""
        if principal.owner_id is None:
            return ServiceResponse(403, {"error": "stable principal is required"})
        deleted = self._upload_repository.delete(principal.owner_id, upload_id)
        if not deleted:
            return ServiceResponse(404, {"error": f"upload not found: {upload_id}"})
        return ServiceResponse(200, {"upload_id": upload_id, "deleted": True})

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
                    _catalog_dataset_body(
                        item,
                        _requires_service_key(item, auth_provider_names),
                    )
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
        sample_mode: str = "first",
        seed: int = DEFAULT_PREVIEW_SEED,
        principal: Principal | None = None,
    ) -> ServiceResponse:
        """각 소스의 스키마와 샘플 행, Source↔Silver diff를 산출한다 (파일 미기록, #497)."""
        if limit < 1 or limit > MAX_PREVIEW_LIMIT:
            return ServiceResponse(
                400, {"error": f"'limit' must be a positive integer up to {MAX_PREVIEW_LIMIT}"}
            )
        if sample_mode not in ("first", "random"):
            return ServiceResponse(400, {"error": "'sample_mode' must be 'first' or 'random'"})
        if not isinstance(seed, int) or isinstance(seed, bool):
            return ServiceResponse(400, {"error": "'seed' must be an integer"})
        spec_or_error = self._load_validated(spec_yaml)
        if isinstance(spec_or_error, ServiceResponse):
            return spec_or_error

        # provider credential은 kind="public_api" source에만 의미가 있다(#498) —
        # file/url source의 provider는 항상 빈 문자열이므로 섞으면 credential
        # resolver에 의미 없는 provider 이름이 전달된다.
        provider_names = tuple(
            source.provider for source in spec_or_error.sources if source.kind == "public_api"
        )
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
            result = preview_build(
                spec_or_error,
                client=client,
                limit=limit,
                sample_mode=cast(SampleMode, sample_mode),
                seed=seed,
                upload_repository=self._upload_repository_for(spec_or_error),
                owner_id=principal.owner_id if principal is not None else None,
            )
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
                "source_sample": list(p.source_sample),
                "sample_mode": p.sample_mode,
                "diff_available": p.diff_available,
                "diffs": cast(
                    JsonValue,
                    [
                        {
                            "row": d.row,
                            "column": d.column,
                            "before": d.before,
                            "after": d.after,
                            "transform": d.transform,
                        }
                        for d in p.diffs
                    ],
                ),
                "transform_summary": (
                    {
                        "changed_cells": p.transform_summary.changed_cells,
                        "changed_rows": p.transform_summary.changed_rows,
                    }
                    if p.transform_summary is not None
                    else None
                ),
                "diff_truncated": p.diff_truncated,
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
        manifest_owner_id: str | None = None,
        principal: Principal | None = None,
        cancellation: CancellationProbe | None = None,
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

        ``manifest_owner_id``는 persisted manifest ownership(및 그 manifest를
        읽어 채우는 BuildIndex, #505 SSOT)에만 쓰이는 별도 값이다 — ``owner_id``
        (kind="file" source resolver의 업로드 소유권 확인용, #498)와 분리하기
        위한 것으로, 생략하면 ``owner_id``를 그대로 쓴다(기존 동작과 동일).
        async run(``_run_build_job``)이 이 필드로 submitting principal의
        owner_id를 manifest/BuildIndex에는 기록하면서도 file resolver에는
        여전히 owner_id를 넘기지 않는 데 쓴다(#496 follow-up).

        ``cancellation``은 async job(#481)에서만 전달되는 협력적 취소 probe다.
        ``None``이면(동기 ``POST /build``, CLI) 파이프라인이 취소 점검을 전혀
        하지 않아 기존 동작과 100% 동일하다 — 동기 build에 취소 상태를 강요하지
        않는다. 취소로 끝난 run은 409와 함께 ``status="cancelled"`` 요약을
        반환하는데, 이 응답은 async worker(``AsyncBuildExecutor._run``)만 보고
        job snapshot에도 실리지 않으므로 HTTP wire에는 노출되지 않는다 —
        부분 산출물의 정본은 partial manifest다.
        """
        spec_or_error = self._load_validated(spec_yaml)
        if isinstance(spec_or_error, ServiceResponse):
            return spec_or_error

        # provider credential은 kind="public_api" source에만 의미가 있다(#498) —
        # file/url source의 provider는 항상 빈 문자열이다.
        provider_names = tuple(
            source.provider for source in spec_or_error.sources if source.kind == "public_api"
        )
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
                manifest_owner_id=manifest_owner_id,
                upload_repository=self._upload_repository_for(spec_or_error),
                event_store=self._event_store,
                cancellation=cancellation,
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
        # 취소된 run(#481)은 성공도 실패도 아니다. 이 응답은 async worker만 보고
        # job snapshot(BuildJob.response)에도 실리지 않으므로 wire 계약(200/502)을
        # 넓히지 않는다 — outcomes도 싣지 않아 SourceOutcome enum(ok/failed)과
        # 어긋나지 않는다. 부분 산출물의 정본은 아래에서 이미 기록된 partial
        # manifest다.
        cancelled = result.status == "cancelled"
        status_code = 200 if result.status == "ok" else 409 if cancelled else 502
        body: dict[str, JsonValue] = {
            "status": result.status,
            "run_id": result.context.run_id,
            "manifest": str(result.manifest_path),
            "api_version": API_CONTRACT_VERSION,
        }
        if not cancelled:
            body["outcomes"] = outcomes
        # composition(#506) 결과는 outcomes(소스별)와 별도로 노출한다 — bronze/silver/gold
        # stage 개념이 없고 "combined result"로 명확히 구분되어야 하기 때문이다.
        # BuildSpec.composition이 없으면 null이다.
        if result.composition_outcome is not None:
            body["composition"] = {
                "name": result.composition_outcome.name,
                "status": result.composition_outcome.status,
                "error": _redact_secret_text(result.composition_outcome.error, secret_values),
            }
        else:
            body["composition"] = None
        # 실패한 빌드는 첫 번째 실패 outcome의 error를 최상위 `error` 요약으로 노출해,
        # Studio 같은 소비자가 outcomes 배열을 파싱하지 않고도 사람이 읽을 수 있는
        # 사유를 즉시 표면화할 수 있게 한다 (#226). composition만 실패하고 모든 source가
        # 성공한 경우도 놓치지 않도록 composition_outcome도 함께 살핀다 (#506).
        if result.status == "failed":
            first_error = next(
                (o.error for o in result.outcomes if o.status != "ok" and o.error), None
            )
            if first_error is None and result.composition_outcome is not None:
                first_error = result.composition_outcome.error
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
        self,
        spec_yaml: str,
        *,
        run_id: str | None = None,
        created_by: str | None = None,
        owner_id: str | None = None,
    ) -> ServiceResponse:
        """비동기 build job을 큐에 넣고 초기 상태를 반환한다 (#482).

        ``owner_id``는 job registry snapshot에 보존된다 — wire 응답
        (``to_body()``)에는 노출되지 않는다. registry에 남은 이 값은 두 곳에
        쓰인다: (1) active run(``check_active_run_access``, #496 follow-up)
        ownership 판정, (2) ``_run_build_job``이 이를 ``build()``의
        ``manifest_owner_id``로 넘겨 persisted manifest/BuildIndex(#505 SSOT)
        에 정확한 owner_id를 기록. ``build()``의 ``owner_id``(``kind="file"``
        source resolver용, #498)에는 여전히 전달하지 않는다 — async
        file-backed source owner propagation 한계는 그대로 유지된다.
        """
        resolved_run_id = run_id or generate_run_id()
        if self._build_index.get(resolved_run_id) is not None:
            return ServiceResponse(
                409,
                {
                    "error": "run_id already completed",
                    "run_id": resolved_run_id,
                },
            )

        def _record_run_submitted() -> None:
            # AsyncBuildExecutor.submit()이 job을 worker pool에 큐잉하기 *전에*
            # 호출된다(#496) — "existing"/"queue_full"이면 새 submission이 아니므로
            # 아예 호출되지 않는다. store.append()는 실패를 삼키지 않고 그대로
            # 전파한다(BuildEventStore, event timeline의 유일한 정본) — 여기서는
            # 그 전파를 그대로 둔다. job이 아직 큐잉되지 않은 시점이라, 이 event가
            # 기록되지 못하면 job도 만들어지지 않는다: "event는 유실됐는데 job은
            # 이미 실행 중"이라는 모순이 생기지 않는다(recorder의 흡수 정책과
            # 달리, 여기는 아직 다른 정본을 침범할 real side effect가 없다).
            self._event_store.append(
                BuildEvent(
                    seq=0,
                    timestamp=datetime.now(tz=timezone.utc),
                    run_id=resolved_run_id,
                    event="run_submitted",
                    status="ok",
                    message="build accepted for async execution",
                )
            )

        def _record_enqueue_failure() -> None:
            # registry.mark_failed() 직후, 예외 재전파 *전에* 호출된다(#496
            # lifecycle 계약: timeline 자체도 이 실패를 표현해야 한다) —
            # run_submitted는 이미 기록됐으니 지우지 않고(append-only), 기존
            # "run_failed" vocabulary로 동일 run_id에 종결 event를 하나 더
            # 남긴다. raw exception/stack trace는 담지 않는다 — bounded,
            # 안전한 고정 message만 쓴다(recorder의 다른 event들과 동일한
            # 방어 원칙). 이 append 자체가 실패해도 로그만 남기고 흡수한다 —
            # 이미 registry가 "failed"로 확정된 뒤라, 이 부가 event 기록
            # 실패가 원래 enqueue 실패(재전파될 예외)를 가리면 안 된다.
            try:
                self._event_store.append(
                    BuildEvent(
                        seq=0,
                        timestamp=datetime.now(tz=timezone.utc),
                        run_id=resolved_run_id,
                        event="run_failed",
                        status="fail",
                        message="build could not be queued for execution",
                    )
                )
            except Exception:
                logger.error(
                    "failed to record run_failed event after enqueue failure (run_id=%s)",
                    resolved_run_id,
                    exc_info=True,
                )

        try:
            result = self._async_builds.submit(
                spec_yaml=spec_yaml,
                run_id=resolved_run_id,
                created_by=created_by,
                owner_id=owner_id,
                runner=self._run_build_job,
                on_accept=_record_run_submitted,
                on_enqueue_failure=_record_enqueue_failure,
            )
        except Exception:
            # run_submitted event append 실패, 또는 event는 기록됐지만 이후
            # worker pool 큐잉(executor.submit) 자체가 실패한 경우 둘 다
            # 여기로 온다 — 두 경우 모두 job은 실행되지 않으므로 202를 주지
            # 않는다(#496).
            logger.error(
                "failed to accept build submission; build was not queued (run_id=%s)",
                resolved_run_id,
                exc_info=True,
            )
            return ServiceResponse(500, {"error": "failed to accept build submission"})
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

    def cancel_build(self, run_id: str) -> ServiceResponse:
        """active(queued/running) async build job의 취소를 요청한다 (#481).

        호출 전에 route가 run_id 형식 검증과 ownership 게이팅을 끝내 놓아야 한다
        (``routes/builds.py`` — ``GET /builds/{run_id}``와 동일한 canonical 규칙).

        응답은 registry의 원자적 판정(``request_cancel``) 하나로 결정되므로
        경합과 무관하게 결정적이다.

        - ``queued`` → 즉시 ``cancelled``(runner를 한 번도 실행하지 않는다), 200.
        - ``running`` → ``cancelling``, 200. 실제 종결은 다음 안전 경계다.
        - 이미 ``cancelling``/``cancelled`` → 200(멱등, 현재 snapshot 그대로).
        - ``succeeded``/``failed``이거나 pipeline이 이미 정상 종료로 확정한 job
          → 409. 새 오류 어휘를 만들지 않고 기존 conflict 관례를 쓴다
          (``POST /builds``의 "이미 완료된 run_id" 409와 같은 의미의 충돌이다).
        - registry가 모르는 run → 404 (``GET /builds/{run_id}``와 동일 메시지).
        """
        outcome, snapshot = self._async_builds.request_cancel(run_id)
        if outcome == "unknown" or snapshot is None:
            return ServiceResponse(404, {"error": f"build job not found: {run_id}"})
        if outcome == "terminal":
            return ServiceResponse(
                409,
                {
                    "error": "build job is no longer cancellable",
                    "run_id": run_id,
                    "status": snapshot.status,
                },
            )
        if outcome == "cancelled":
            # queued job은 여기서 이미 종결됐다 — worker는 runner를 실행하지
            # 않는다(``AsyncBuildExecutor._run``의 ``begin_run`` 게이트). 종결
            # event는 running 경로와 동일하게 terminal 전이 시점에 한 번 남긴다.
            self._record_run_cancelled(run_id)
        return ServiceResponse(200, snapshot.to_body())

    def _record_run_cancelled(self, run_id: str) -> None:
        """cancelled 종결 event를 남긴다 (#481). 실패해도 전파하지 않는다.

        job은 이 시점에 이미 ``cancelled``로 확정돼 있다 — event append 실패가
        확정된 종단 상태를 되돌릴 수는 없으므로, ``_record_enqueue_failure``와
        동일하게 로그만 남기고 흡수한다. worker thread에서도 호출되므로 예외를
        전파하면 안 된다. message는 고정 문자열이며 raw exception/경로/자격증명을
        담지 않는다.
        """
        try:
            self._event_store.append(
                BuildEvent(
                    seq=0,
                    timestamp=datetime.now(tz=timezone.utc),
                    run_id=run_id,
                    event="run_cancelled",
                    status="ok",
                    message="build cancelled at a safe stage boundary",
                )
            )
        except Exception:
            logger.error("failed to record run_cancelled event (run_id=%s)", run_id, exc_info=True)

    def _run_build_job(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        cancellation: CancellationProbe,
    ) -> ServiceResponse:
        """async job registry가 부르는 실제 실행 진입점 (#482, #496 follow-up).

        ``build()``에 ``owner_id``는 전달하지 않는다(``None`` 그대로) —
        ``kind="file"`` source resolver는 async 경로에서 여전히 stable owner
        identity를 알지 못한다(#498 async limitation 유지). 대신
        ``manifest_owner_id``로 registry snapshot에 이미 보존된(``submit_build``
        가 채운) submitting principal의 owner_id를 넘긴다 — persisted manifest
        (및 그 manifest를 그대로 읽어 채우는 BuildIndex, #505 SSOT)는
        ``build()`` 내부에서 단 한 번의 write로 정확한 owner_id를 갖게 되고,
        build 종료 후 manifest를 별도로 사후 수정할 필요가 없다.
        """
        snapshot = self._async_builds.get(run_id)
        manifest_owner_id = snapshot.owner_id if snapshot is not None else None
        return self.build(
            spec_yaml,
            run_id=run_id,
            created_by=created_by,
            manifest_owner_id=manifest_owner_id,
            # 협력적 취소 probe(#481)를 그대로 pipeline까지 내려보낸다 — service
            # 개념(registry/HTTP/Principal)은 pipeline domain으로 넘기지 않는다.
            cancellation=cancellation,
        )

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
                    # manifest.json이 정본이므로 파생 규칙은 한 곳에만 둔다
                    # (#481) — cancelled run은 errors가 비어 있을 수 있어
                    # 기존 "errors 유무" 파생만으로는 ok로 잘못 보고된다.
                    "status": status_from_manifest(manifest),
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

    def monitoring_summary(self) -> ServiceResponse:
        """Builder API/Queue/Worker/Artifact Store 시스템 상태 요약 (#516).

        시스템 aggregate만 담으며 개별 run의 dataset/owner/credential 정보는
        포함하지 않는다 — ownership 필터링이 필요 없다. Provider status(#492)는
        요청마다 실제 네트워크 프로브를 유발하므로 이번 PR에서는 포함하지 않는다.
        """
        api = monitoring_service.api_status(self._latency_recorder)
        queue = monitoring_service.queue_status(self._async_builds)
        workers = monitoring_service.worker_status(self._async_builds)
        artifact_store = monitoring_service.artifact_store_status(
            self._output_root, self._build_index
        )
        status = monitoring_service.aggregate_status(
            api=api, queue=queue, workers=workers, artifact_store=artifact_store
        )
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return ServiceResponse(
            200,
            {
                "generated_at": generated_at,
                "status": status,
                "api": {
                    "availability": api.availability,
                    "sample_count": api.sample_count,
                    "p95_latency_ms": api.p95_latency_ms,
                },
                "queue": {
                    "availability": queue.availability,
                    "waiting": queue.waiting,
                    "running": queue.running,
                    "total": queue.total,
                },
                "workers": {
                    "availability": workers.availability,
                    "active": workers.active,
                    "capacity": workers.capacity,
                    "utilization": workers.utilization,
                },
                "artifact_store": {
                    "availability": artifact_store.availability,
                    "last_write_at": artifact_store.last_write_at,
                },
            },
        )

    def monitoring_builds(
        self, *, window: str, bucket: str, principal: Principal | None = None
    ) -> ServiceResponse:
        """window/bucket별 build 통계와 recent runs를 반환한다 (#516).

        ENFORCE_OWNERSHIP+oidc principal일 때는 본인이 접근 가능한 run만
        집계·노출한다(#505) — 다른 principal의 run metadata가 새는 side
        channel이 되지 않는다.
        """
        validated_window = monitoring_service.validate_window(window)
        if validated_window is None:
            return ServiceResponse(400, {"error": f"unsupported window: {window!r} (only '24h')"})
        validated_bucket = monitoring_service.validate_bucket(bucket)
        if validated_bucket is None:
            return ServiceResponse(400, {"error": f"unsupported bucket: {bucket!r} (only 'hour')"})

        stats = monitoring_service.build_statistics(
            self._build_index,
            window=validated_window,
            bucket=validated_bucket,
            principal=principal,
            enforce_ownership=_enforce_ownership(),
        )
        buckets: list[JsonValue] = [
            {
                "bucket_start": b.bucket_start,
                "bucket_end": b.bucket_end,
                "total": b.total,
                # wire 계약은 success/failed/cancelled다(#527) — 내부 BuildIndex
                # status 값 "ok"는 그대로 두고 외부 필드 이름만 매핑한다.
                "success": b.success,
                "failed": b.failed,
                "cancelled": b.cancelled,
            }
            for b in stats.buckets
        ]
        recent_runs: list[JsonValue] = [
            {
                "run_id": r.run_id,
                "status": r.status,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
            }
            for r in stats.recent_runs
        ]
        return ServiceResponse(
            200,
            {
                "window": stats.window,
                "bucket": stats.bucket,
                "availability": stats.availability,
                "excluded_count": stats.excluded_count,
                "buckets": buckets,
                "recent_runs": recent_runs,
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

    def _publish_context(
        self, run_id: str
    ) -> tuple[publish_service.RunStatus, dict[str, JsonValue] | None, BuildSpec | None]:
        """publish readiness/POST가 공유하는 (status, manifest, spec) 조회.

        route adapter가 이미 존재/ownership을 판정했다는 전제 아래(``/stages``,
        ``/quality``와 동일한 패턴) 호출된다. manifest가 있으면 그것이 정본
        (terminal run)이고, 없으면 async job registry(#482)에서 active/terminal
        상태를 읽는다 — ``routes._guards.check_active_run_access``와 동일한
        두 소스를 쓴다(#496 follow-up 패턴 재사용).
        """
        manifest = cast(
            "dict[str, JsonValue] | None", datasets_service.read_manifest(self._output_root, run_id)
        )
        if manifest is not None:
            status: publish_service.RunStatus = "failed" if manifest.get("errors") else "succeeded"
            spec = datasets_service.read_snapshot_spec(self._output_root, run_id)
            return status, manifest, spec
        snapshot = self._async_builds.get(run_id)
        if snapshot is not None:
            return snapshot.status, None, None
        # route adapter의 check_active_run_access가 이미 존재를 보장했으므로
        # 이론상 도달하지 않는다 — fail-closed로 failed 취급한다.
        return "failed", None, None

    def publish_readiness(self, run_id: str, target: str, destination: str | None = None) -> ServiceResponse:
        """GET /builds/{run_id}/publish/readiness (#491).

        side-effect-free다 — Publisher를 호출하거나 원격 dataset을 만들지
        않는다. ready == blockers가 하나도 없음으로 deterministic하게 계산한다.
        """
        resolved_target, error = publish_service.resolve_target(target)
        if resolved_target is None:
            return ServiceResponse(
                400,
                {"error": error or "invalid target", "code": "unsupported_target"},
            )

        status, manifest, spec = self._publish_context(run_id)
        result = publish_service.build_readiness(
            run_id=run_id,
            target=resolved_target,
            status=status,
            manifest=cast("dict[str, object] | None", manifest),
            spec=spec,
            output_root=self._output_root,
            destination=destination,
        )
        return ServiceResponse(
            200,
            {
                "run_id": run_id,
                "target": result.target,
                "ready": result.ready,
                "blockers": cast(JsonValue, [b.to_body() for b in result.blockers]),
                "warnings": cast(JsonValue, [w.to_body() for w in result.warnings]),
            },
        )

    def publish(
        self,
        run_id: str,
        body: Mapping[str, JsonValue] | None,
        *,
        principal: Principal,
    ) -> ServiceResponse:
        """POST /builds/{run_id}/publish (#491).

        readiness와 완전히 같은 deterministic 검사를 다시 수행한다 — 호출자가
        먼저 GET readiness를 불렀다고 신뢰하지 않는다(TOCTOU: readiness 통과
        이후 상태가 바뀌어도 여기서 다시 막힌다). blocker가 하나라도 있으면
        기존 Publisher를 절대 호출하지 않는다.
        """
        if not isinstance(body, Mapping):
            return ServiceResponse(400, {"error": "request body must be a JSON object"})

        unknown_fields = sorted(
            str(key) for key in body if key not in {"target", "destination", "options"}
        )
        if unknown_fields:
            return ServiceResponse(
                400, {"error": f"unsupported request field(s): {unknown_fields!r}"}
            )

        resolved_target, error = publish_service.resolve_target(body.get("target"))
        if resolved_target is None:
            return ServiceResponse(
                400,
                {"error": error or "invalid target", "code": "unsupported_target"},
            )

        destination_error = publish_service.validate_destination(
            resolved_target, body.get("destination")
        )
        if destination_error is not None:
            return ServiceResponse(400, {"error": destination_error})
        destination = cast(str, body["destination"])

        options_error, options = publish_service.validate_options(
            resolved_target, body.get("options")
        )
        if options_error is not None:
            return ServiceResponse(400, {"error": options_error})

        owner_key = principal.owner_id or principal.label
        try:
            existing = self._publish_receipts.lookup(
                owner_key=owner_key,
                run_id=run_id,
                target=resolved_target,
                destination=destination,
                options=options,
            )
        except Exception as exc:
            logger.error(
                "publish receipt lookup failed: run_id=%s target=%s error_type=%s",
                run_id,
                resolved_target,
                type(exc).__name__,
            )
            return ServiceResponse(
                409,
                {
                    "error": "publish operation state is unavailable; retry is blocked",
                    "code": "publish_state_unknown",
                },
            )
        if existing is not None:
            existing_response = _publish_receipt_response(*existing)
            if existing_response is not None:
                return existing_response

        status, manifest, spec = self._publish_context(run_id)
        readiness = publish_service.build_readiness(
            run_id=run_id,
            target=resolved_target,
            status=status,
            manifest=cast("dict[str, object] | None", manifest),
            spec=spec,
            output_root=self._output_root,
            destination=destination,
        )
        if not readiness.ready or readiness.artifacts is None:
            return ServiceResponse(
                409,
                {
                    "error": f"run is not ready to publish to {resolved_target!r}",
                    "blockers": cast(JsonValue, [b.to_body() for b in readiness.blockers]),
                },
            )

        try:
            claim_status, receipt = self._publish_receipts.claim(
                owner_key=owner_key,
                run_id=run_id,
                target=resolved_target,
                destination=destination,
                options=options,
            )
        except Exception as exc:
            logger.error(
                "publish receipt claim failed: run_id=%s target=%s error_type=%s",
                run_id,
                resolved_target,
                type(exc).__name__,
            )
            return ServiceResponse(
                409,
                {
                    "error": "publish operation state is unavailable; retry is blocked",
                    "code": "publish_state_unknown",
                },
            )

        claimed_response = _publish_receipt_response(claim_status, receipt)
        if claimed_response is not None:
            return claimed_response

        # kaggle target인 경우 metadata override (#550)
        if resolved_target == "kaggle":
            publish_service.override_kaggle_metadata_id(readiness.artifacts, destination)

        publisher = PUBLISHER_REGISTRY[resolved_target]
        publish_kwargs: dict[str, object] = {"destination": destination, **options}
        try:
            result = publisher.publish(readiness.artifacts.paths, **publish_kwargs)  # type: ignore[arg-type]
        except Exception as exc:
            # Publisher가 던지는 예외(PublishError, credential/dependency
            # RuntimeError, 그 외 검토하지 않은 예외 포함)는 어떤 것도 "안전한
            # known exception"으로 취급하지 않는다 — 외부 SDK 예외 메시지에는
            # 원격 응답 원문이나(#491 지침 1) 로컬 filesystem 절대 경로가 섞일
            # 수 있다. client에는 항상 stable generic 메시지만 보낸다.
            #
            # 서버 log도 str(exc)/repr(exc)나 traceback(로그에 다시 raw
            # message를 남기는 logger.exception())을 쓰지 않는다 — exception
            # type과 이미 안전하다고 확인된 context만 남긴다.
            logger.error(
                "publish failed: run_id=%s target=%s error_type=%s",
                run_id,
                resolved_target,
                type(exc).__name__,
            )
            try:
                self._publish_receipts.mark_unknown(receipt.fingerprint)
            except Exception as receipt_exc:
                logger.error(
                    "publish receipt unknown-state persist failed: "
                    "run_id=%s target=%s error_type=%s",
                    run_id,
                    resolved_target,
                    type(receipt_exc).__name__,
                )
            return ServiceResponse(
                502,
                {"error": "publish failed due to an unexpected error", "code": "publish_failed"},
            )

        response_body: dict[str, JsonValue] = {
            "run_id": run_id,
            "target": resolved_target,
            "publisher": result.publisher,
            "destination": destination,
            "reference": result.reference,
            "artifact_count": result.artifact_count,
            "status": result.status,
        }
        try:
            self._publish_receipts.mark_succeeded(
                receipt.fingerprint, cast(dict[str, object], response_body)
            )
        except Exception as exc:
            logger.error(
                "publish receipt success persist failed: run_id=%s target=%s error_type=%s",
                run_id,
                resolved_target,
                type(exc).__name__,
            )
            with suppress(Exception):
                self._publish_receipts.mark_unknown(receipt.fingerprint)
            return ServiceResponse(
                502,
                {"error": "publish failed due to an unexpected error", "code": "publish_failed"},
            )
        return ServiceResponse(200, response_body)

    def get_publish_receipt(
        self,
        run_id: str,
        target: str,
        destination: str,
        *,
        principal: Principal,
    ) -> ServiceResponse:
        """GET /builds/{run_id}/publish/receipt (#551).

        unknown receipt로 영구 차단된 운영자가 상태를 조회한다. 소유자 불일치는
        404로 응답해 다른 owner의 receipt 존재 자체를 노출하지 않는다.
        """
        owner_key = principal.owner_id or principal.label
        receipt = self._publish_receipts.get_by_key(
            owner_key=owner_key, run_id=run_id, target=target, destination=destination
        )
        if receipt is None:
            return ServiceResponse(
                404, {"error": "publish receipt not found", "code": "receipt_not_found"}
            )
        body: dict[str, JsonValue] = {
            "run_id": run_id,
            "target": receipt.target,
            "destination": receipt.destination,
            "state": receipt.state,
            "fingerprint": receipt.fingerprint,
            "options": cast(JsonValue, receipt.options),
            "reconcilable": receipt.state == "unknown",
        }
        if receipt.result is not None:
            body["result"] = cast(JsonValue, receipt.result)
        return ServiceResponse(200, body)

    def reconcile_publish(
        self,
        run_id: str,
        body: Mapping[str, JsonValue] | None,
        *,
        principal: Principal,
    ) -> ServiceResponse:
        """POST /builds/{run_id}/publish/reconcile (#551).

        unknown receipt를 원격 상태 확인으로 확정한다. 원격에 결과가 있으면
        succeeded로 확정하고, 확실히 없으면 receipt를 reset해 재게시(새 claim)를
        허용한다. 원격 확인 자체가 불가능하면 503 — 아무 것도 변경하지 않는다.
        """
        if not isinstance(body, Mapping):
            return ServiceResponse(400, {"error": "request body must be a JSON object"})
        unknown_fields = sorted(str(key) for key in body if key not in {"target", "destination"})
        if unknown_fields:
            return ServiceResponse(
                400, {"error": f"unsupported request field(s): {unknown_fields!r}"}
            )

        resolved_target, target_error = publish_service.resolve_target(body.get("target"))
        if resolved_target is None:
            return ServiceResponse(
                400, {"error": target_error or "invalid target", "code": "unsupported_target"}
            )
        destination_error = publish_service.validate_destination(
            resolved_target, body.get("destination")
        )
        if destination_error is not None:
            return ServiceResponse(400, {"error": destination_error})
        destination = cast(str, body["destination"])

        owner_key = principal.owner_id or principal.label
        receipt = self._publish_receipts.get_by_key(
            owner_key=owner_key, run_id=run_id, target=resolved_target, destination=destination
        )
        if receipt is None:
            return ServiceResponse(
                404, {"error": "publish receipt not found", "code": "receipt_not_found"}
            )

        if receipt.state == "succeeded":
            # 이미 확정된 receipt는 멱등하게 그 상태를 돌려준다(원격 재조회 없음).
            body_out: dict[str, JsonValue] = {
                "run_id": run_id,
                "state": "succeeded",
                "reconciled": False,
                "fingerprint": receipt.fingerprint,
            }
            if receipt.result is not None:
                body_out["result"] = cast(JsonValue, receipt.result)
            return ServiceResponse(200, body_out)

        probe = self._probe_remote_publish_target(resolved_target, destination)
        if probe is None:
            return ServiceResponse(
                503,
                {
                    "error": "remote state could not be determined; nothing was changed",
                    "code": "reconcile_unavailable",
                },
            )
        remote_exists = probe

        if remote_exists:
            result: dict[str, object] = {
                "run_id": run_id,
                "target": resolved_target,
                "destination": destination,
                "reconciled": True,
                "status": "succeeded",
            }
            try:
                self._publish_receipts.reconcile_succeeded(receipt.fingerprint, result)
            except Exception as exc:
                logger.error(
                    "publish receipt reconcile persist failed: run_id=%s target=%s error_type=%s",
                    run_id,
                    resolved_target,
                    type(exc).__name__,
                )
                return ServiceResponse(
                    503,
                    {
                        "error": "reconcile outcome could not be persisted; nothing was changed",
                        "code": "reconcile_unavailable",
                    },
                )
            return ServiceResponse(
                200,
                {
                    "run_id": run_id,
                    "state": "succeeded",
                    "reconciled": True,
                    "fingerprint": receipt.fingerprint,
                },
            )

        # 원격에 결과가 없다 — 게시가 실제로 일어나지 않았다고 확정할 수 없어도
        # receipt를 reset해 운영자 판단으로 재게시를 허용한다(감사 로그에 남긴다).
        reset_ok = self._publish_receipts.reset(
            receipt.fingerprint, action="reconcile_absent_reset"
        )
        if not reset_ok:
            return ServiceResponse(
                503,
                {
                    "error": "reconcile reset could not be persisted; nothing was changed",
                    "code": "reconcile_unavailable",
                },
            )
        return ServiceResponse(
            200,
            {
                "run_id": run_id,
                "state": "reset",
                "reconciled": True,
                "retry_allowed": True,
                "fingerprint": receipt.fingerprint,
            },
        )

    def reset_publish_receipt(
        self,
        run_id: str,
        target: str,
        destination: str,
        *,
        principal: Principal,
    ) -> ServiceResponse:
        """DELETE /builds/{run_id}/publish/receipt (#551) — 명시적 reset.

        어떤 상태든 receipt를 삭제해 새 claim을 허용한다. 원격 부작용은 전혀
        발생하지 않는다(이미 게시된 결과를 되돌리지 않는다). 감사 로그에 남긴다.
        """
        owner_key = principal.owner_id or principal.label
        receipt = self._publish_receipts.get_by_key(
            owner_key=owner_key, run_id=run_id, target=target, destination=destination
        )
        if receipt is None:
            return ServiceResponse(
                404, {"error": "publish receipt not found", "code": "receipt_not_found"}
            )
        reset_ok = self._publish_receipts.reset(receipt.fingerprint, action="manual_reset")
        if not reset_ok:
            return ServiceResponse(
                503,
                {
                    "error": "receipt reset could not be persisted; nothing was changed",
                    "code": "reconcile_unavailable",
                },
            )
        return ServiceResponse(
            200,
            {
                "run_id": run_id,
                "state": "reset",
                "retry_allowed": True,
                "fingerprint": receipt.fingerprint,
            },
        )

    def _probe_remote_publish_target(self, target: str, destination: str) -> bool | None:
        """원격에 publish 결과가 존재하는지 조사한다 (#551).

        반환값: True(존재)/False(부재)/None(판단 불가 — credential·네트워크 문제).
        조사 자체가 credential을 소비하거나 원격을 변경하지 않는 read-only다.
        """
        if target == "huggingface":
            token = os.environ.get("HF_TOKEN", "").strip()
            if not token:
                return None
            try:
                from huggingface_hub import HfApi  # type: ignore[import-not-found]
            except ImportError:
                return None
            try:
                api = HfApi(token=token)
                api.dataset_info(repo_id=destination, repo_type="dataset")
            except Exception as exc:
                # repo 부재(gated 401/404 계열)와 접근 실패를 구분한다 —
                # huggingface_hub는 부재를 RepositoryNotFoundError로 알려준다.
                name = type(exc).__name__
                if name in ("RepositoryNotFoundError", "GatedRepoError"):
                    return False
                if getattr(exc, "status_code", None) in (401, 403):
                    # 존재하지 않는 private repo도 401로 보이는 HF 특성상
                    # 소유자라면 부재로 간주한다(asset이 내 credential로
                    # 생성됐다면 접근 가능해야 하기 때문).
                    return False
                if getattr(exc, "status_code", None) == 404:
                    return False
                return None
            return True
        return None

    def get_build_events(self, run_id: str, *, limit: int, tail: bool) -> ServiceResponse:
        """run의 append-only structured event timeline을 조회한다 (#496).

        호출 전에 run_id 검증·존재 확인·ownership 게이팅이 끝나 있어야 한다
        (``/builds/{run_id}/stages``와 동일하게 dispatch route adapter가 먼저
        처리한다). 반환은 항상 chronological ascending이다 — ``tail=True``도
        최신 ``limit``개를 고르되 정렬 자체는 뒤집지 않는다(#496 ordering 정책).
        """
        events = self._event_store.list_for_run(run_id, limit=limit, tail=tail)
        body: dict[str, JsonValue] = {
            "run_id": run_id,
            "events": cast(JsonValue, [events_service.event_to_json(e) for e in events]),
        }
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
    raw_body: bytes | None = None,
) -> ServiceResponse | FileResponse:
    """``_dispatch_impl``을 호출하고 처리 시간을 Monitoring latency 표본으로
    기록한다 (#516).

    타이밍은 라우팅+인증+비즈니스 로직 전체를 감싼다(HTTP 소켓 I/O는 제외 —
    그건 http.py 계층). metric 기록 실패가 요청 실패로 전파되지 않도록
    ``LatencyRecorder.record``가 이미 내부적으로 예외를 흡수한다.

    ``raw_body``는 ``POST /uploads``(#498)에서만 쓰이는 binary body다 — 다른
    모든 endpoint는 JSON ``body``만 사용하며 ``raw_body``는 None이다.
    """
    started = time.perf_counter()
    try:
        return _dispatch_impl(
            service,
            method,
            path,
            body,
            query,
            api_key=api_key,
            bearer_token=bearer_token,
            raw_body=raw_body,
        )
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        service._latency_recorder.record(elapsed_ms)


def _dispatch_impl(
    service: BuilderService,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
    query: str = "",
    *,
    api_key: str | None = None,
    bearer_token: str | None = None,
    raw_body: bytes | None = None,
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

    # /uploads(#498)는 binary body(raw_body)가 필요한 유일한 endpoint라 표준
    # RouteAdapter(JSON body만 받음) 목록에 넣지 않고 여기서 직접 호출한다 —
    # 과거의 거대한 dispatch monolith를 복원하는 것이 아니라, 이 한 endpoint의
    # 전송 형태가 route adapter 계약과 다르기 때문이다.
    uploads_response = uploads_route.handle(
        service, method, path, principal, query=query, raw_body=raw_body
    )
    if uploads_response is not None:
        return uploads_response

    for adapter in ROUTE_ADAPTERS:
        response = adapter(service, method, path, body, query, principal)
        if response is not None:
            return response

    return ServiceResponse(404, {"error": f"not found: {method} {path}"})


__all__ = ["API_CONTRACT_VERSION", "BuilderService", "ServiceResponse", "FileResponse", "dispatch"]
