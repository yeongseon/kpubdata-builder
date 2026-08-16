"""Source kind resolver — public_api/file/url을 동일한 BronzeArtifact로 만든다 (#498).

새 pipeline이 아니라 기존 Bronze→Silver→Gold pipeline 앞에 붙는 resolver
layer다. ``kind`` 에 따라 서로 다른 방식으로 원시 레코드를 얻지만, 세 경로 모두
``build.build_bronze_artifact`` 와 동일한 ``BronzeArtifact`` 모양(raw_records/
fetch_params/fetched_at/provenance)으로 수렴한다 — Silver 이후 단계는 소스가
어떤 kind였는지 전혀 알 필요가 없다.

기존 ``provider.dataset`` provenance 모양을 그대로 재사용한다(#498, "모든
source가 동일 Bronze artifact contract 사용"): file은 ``("file", upload_id)``,
url은 ``("url", <host+path 기반 안전한 slug>)`` 를 provider/dataset 자리에
채운다. manifest/provenance 모델에 새 필드를 추가하지 않고 기존 계약을 그대로
쓴다.

이 ``(provider, dataset)`` 쌍은 provenance 식별자일 뿐 아니라 alias가 없을 때
``pipeline.orchestrator``/``service.stages`` 가 그대로 output/persist 디렉터리
세그먼트(``stages._path_safety.validate_path_segment``)로도 쓴다 — 그래서 url
kind의 dataset은 사람이 읽기보다 "항상 안전한 경로 세그먼트"를 우선한다. 원본
endpoint(query 제거)는 ``sanitize_endpoint_identity()`` 로 별도 계산해
fetch_params/provenance.params에만 담는다(경로에는 쓰이지 않음).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from ...ingestion import IngestionError, parse_tabular_bytes, safe_fetch_get
from ...ingestion.url_fetch import default_max_fetch_bytes
from ...spec import JsonValue, SourceRef
from ...uploads import UploadRepository
from .build import SourceClient, build_bronze_artifact
from .models import BronzeArtifact, ProvenanceEvent, require_timezone_aware, utc_now

_NON_SLUG_CHARS = re.compile(r"[^a-zA-Z0-9]+")


def source_identity(source: SourceRef) -> tuple[str, str]:
    """모든 kind에 대해 (provider, dataset) 자리를 채우는 canonical identity.

    기존 provenance/manifest/output-path 코드가 이미 ``"{provider}.{dataset}"``
    형태를 source identity(및 alias 없을 때의 output 디렉터리 세그먼트)로
    쓰므로, file/url kind도 그 모양에 맞춰 항상 경로로 안전한 값을 만든다.
    """
    if source.kind == "file":
        return "file", source.upload_id
    if source.kind == "url":
        return "url", _url_path_safe_identity(source.endpoint)
    return source.provider, source.dataset


def _url_path_safe_identity(endpoint: str) -> str:
    """endpoint를 ``validate_path_segment`` 를 항상 통과하는 slug로 줄인다.

    URL은 ``:``/``/`` 등 경로 세그먼트로 쓸 수 없는 문자를 포함하므로, host를
    영숫자-하이픈 slug로 정규화하고 (query 제거된) endpoint 전체의 SHA-256
    앞 12자리를 붙여 서로 다른 경로가 우연히 같은 slug로 뭉치지 않게 한다.
    사람이 읽는 endpoint는 이 값이 아니라 ``sanitize_endpoint_identity()`` 가
    fetch_params/provenance.params에 별도로 담는다.
    """
    sanitized = sanitize_endpoint_identity(endpoint)
    host = urlsplit(sanitized).hostname or "host"
    slug = _NON_SLUG_CHARS.sub("-", host).strip("-") or "host"
    digest = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


def sanitize_endpoint_identity(endpoint: str) -> str:
    """endpoint에서 query string/userinfo/fragment를 제거한 사람이 읽는 identity를 만든다.

    url source의 P0는 Auth=None이라 endpoint 자체에 secret이 실릴 일이 적지만,
    query string에 우연히 token 등이 섞였을 때도 provenance/manifest에 남지
    않도록 방어적으로 제거한다("provenance endpoint secret 제거", #498). 경로
    세그먼트로 쓰기에는 안전하지 않다 — 그 용도는 ``_url_path_safe_identity``.
    """
    parts = urlsplit(endpoint)
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path or "/", "", ""))


def build_bronze_artifact_for_source(
    source: SourceRef,
    *,
    client: SourceClient,
    upload_repository: UploadRepository | None = None,
    owner_id: str | None = None,
    fetched_at: datetime | None = None,
) -> BronzeArtifact:
    """source.kind에 맞는 방식으로 fetch해 BronzeArtifact를 만든다 (#498).

    매개변수:
        source: canonical source 참조 (public_api/file/url).
        client: public_api kind에서만 쓰이는 kpubdata 호환 client.
        upload_repository: file kind에서 업로드 content를 조회할 저장소.
        owner_id: file kind에서 업로드 소유권을 확인할 principal의 stable id.
        fetched_at: fetch 완료 시각. 생략 시 현재 UTC.

    반환값:
        BronzeArtifact: kind와 무관하게 동일한 모양의 산출물.

    예외:
        IngestionError: file/url fetch·파싱 실패, 소유권 없음, 저장소 미설정.
    """
    if source.kind == "file":
        return _build_from_upload(
            source, upload_repository=upload_repository, owner_id=owner_id, fetched_at=fetched_at
        )
    if source.kind == "url":
        return _build_from_url(source, fetched_at=fetched_at)
    provider, dataset = source_identity(source)
    return build_bronze_artifact(
        client,
        source_key=f"{provider}.{dataset}",
        fetch_params=dict(source.params),
        fetched_at=fetched_at,
    )


def _finalize(
    *,
    provider: str,
    dataset: str,
    records: tuple[dict[str, JsonValue], ...],
    fetch_params: dict[str, JsonValue],
    fetched_at: datetime | None,
) -> BronzeArtifact:
    resolved_fetched_at = fetched_at or utc_now()
    require_timezone_aware(resolved_fetched_at, field_name="fetched_at")
    source_key = f"{provider}.{dataset}"
    provenance = ProvenanceEvent(
        source_key=source_key, fetch_params=fetch_params, fetched_at=resolved_fetched_at
    )
    return BronzeArtifact(
        source_key=source_key,
        raw_records=records,
        fetch_params=fetch_params,
        fetched_at=resolved_fetched_at,
        provenance=provenance,
    )


def _build_from_upload(
    source: SourceRef,
    *,
    upload_repository: UploadRepository | None,
    owner_id: str | None,
    fetched_at: datetime | None,
) -> BronzeArtifact:
    if upload_repository is None:
        raise IngestionError("file source requires an upload store to be configured")
    if not owner_id:
        raise IngestionError("file source requires an authenticated, stable principal owner")
    metadata = upload_repository.get_metadata(owner_id, source.upload_id)
    if metadata is None:
        # 존재하지 않는 upload_id와 "principal 소유가 아닌" upload_id를
        # 구분하지 않는다 — 다른 사용자의 upload 존재 여부를 흘리지 않는다
        # (fail-closed, #505의 ownership 패턴과 동일).
        raise IngestionError(f"upload not found: {source.upload_id}")
    if metadata.format != source.format or metadata.encoding != source.encoding:
        raise IngestionError(
            "sources[].format/encoding does not match the stored upload "
            f"(upload format={metadata.format!r} encoding={metadata.encoding!r})"
        )
    content = upload_repository.get_content(owner_id, source.upload_id)
    if content is None:
        raise IngestionError(f"upload not found: {source.upload_id}")
    records = parse_tabular_bytes(content, format=source.format, encoding=source.encoding)
    provider, dataset = source_identity(source)
    fetch_params: dict[str, JsonValue] = {
        "upload_id": source.upload_id,
        "format": source.format,
        "encoding": source.encoding,
    }
    return _finalize(
        provider=provider,
        dataset=dataset,
        records=records,
        fetch_params=fetch_params,
        fetched_at=fetched_at,
    )


def _build_from_url(source: SourceRef, *, fetched_at: datetime | None) -> BronzeArtifact:
    result = safe_fetch_get(source.endpoint, max_bytes=default_max_fetch_bytes())
    resolved_format = source.format or _infer_format(result.content_type) or "json"
    records = parse_tabular_bytes(result.content, format=resolved_format, encoding="utf-8")
    provider, dataset = source_identity(source)
    # fetch_params.endpoint는 사람이 읽는 (query 제거된) 원본 endpoint다 — path
    # 세그먼트로 쓰이는 `dataset`(slug+hash, source_identity 참고)과는 다른 값이다.
    fetch_params: dict[str, JsonValue] = {
        "endpoint": sanitize_endpoint_identity(source.endpoint),
        "method": source.method,
    }
    return _finalize(
        provider=provider,
        dataset=dataset,
        records=records,
        fetch_params=fetch_params,
        fetched_at=fetched_at,
    )


def _infer_format(content_type: str) -> str | None:
    """명시적 ``source.format`` 이 없을 때만 Content-Type로 포맷을 추정한다."""
    normalized = content_type.split(";", 1)[0].strip().lower()
    if normalized == "application/json":
        return "json"
    if normalized in ("application/x-ndjson", "application/jsonl"):
        return "jsonl"
    if normalized in ("text/csv", "application/csv"):
        return "csv"
    return None


__all__ = ["build_bronze_artifact_for_source", "sanitize_endpoint_identity", "source_identity"]
