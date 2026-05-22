"""브론즈 단계 소스 가져오기 도우미."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from ...spec import JsonValue
from .models import BronzeArtifact, ProvenanceEvent, require_timezone_aware, utc_now


class DatasetResult(Protocol):
    """호환되는 kpubdata 데이터셋이 반환하는 최소 결과 형태."""

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        """가져온 레코드를 반환한다."""
        ...


class SourceDataset(Protocol):
    """브론즈 단계에서 사용하는 최소 데이터셋 형태."""

    def list(self, **params: JsonValue) -> DatasetResult:
        """하나의 파라미터 집합에 대해 레코드를 가져온다."""
        ...


class SourceClient(Protocol):
    """브론즈 단계에서 사용하는 최소 클라이언트 형태."""

    def dataset(self, source_key: str) -> SourceDataset:
        """소스 키에 대한 데이터셋 객체를 반환한다."""
        ...


def build_bronze_artifact(
    client: SourceClient,
    *,
    source_key: str,
    fetch_params: dict[str, JsonValue] | None = None,
    fetched_at: datetime | None = None,
) -> BronzeArtifact:
    """호환 클라이언트에서 원시 레코드를 가져와 브론즈 산출물을 반환한다."""
    resolved_params = dict(fetch_params or {})
    resolved_fetched_at = fetched_at or utc_now()
    require_timezone_aware(resolved_fetched_at, field_name="fetched_at")

    dataset = client.dataset(source_key)
    result = dataset.list(**resolved_params)
    raw_records = tuple(result.items)
    provenance = ProvenanceEvent(
        source_key=source_key,
        fetch_params=resolved_params,
        fetched_at=resolved_fetched_at,
    )

    return BronzeArtifact(
        source_key=source_key,
        raw_records=raw_records,
        fetch_params=resolved_params,
        fetched_at=resolved_fetched_at,
        provenance=provenance,
    )
