"""Bronze stage source fetch helpers."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from ...spec import JsonValue
from .models import (
    BronzeArtifact,
    ProvenanceEvent,
    compute_data_checksum,
    require_timezone_aware,
    utc_now,
)


class DatasetResult(Protocol):
    """Minimal result shape returned by compatible kpubdata datasets."""

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        """Return fetched records."""


class SourceDataset(Protocol):
    """Minimal dataset shape used by the Bronze stage."""

    def list(self, **params: JsonValue) -> DatasetResult:
        """Fetch records for one parameter set."""


class SourceClient(Protocol):
    """Minimal client shape used by the Bronze stage."""

    def dataset(self, source_key: str) -> SourceDataset:
        """Return a dataset object for a source key."""


def build_bronze_artifact(
    client: SourceClient,
    *,
    source_key: str,
    fetch_params: dict[str, JsonValue] | None = None,
    fetched_at: datetime | None = None,
) -> BronzeArtifact:
    """Fetch raw records from a compatible client and return a Bronze artifact."""
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
        record_count=len(raw_records),
        data_checksum=compute_data_checksum(raw_records),
    )

    return BronzeArtifact(
        source_key=source_key,
        raw_records=raw_records,
        fetch_params=resolved_params,
        fetched_at=resolved_fetched_at,
        provenance=provenance,
    )
