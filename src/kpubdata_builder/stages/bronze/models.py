"""Bronze stage artifact and provenance models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ...spec import JsonValue


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(tz=timezone.utc)


def require_timezone_aware(value: datetime, *, field_name: str) -> None:
    """Validate that a datetime includes timezone information."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True)
class ProvenanceEvent:
    """Record where and when a Bronze source fetch happened."""

    source_key: str
    fetch_params: dict[str, JsonValue] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=utc_now)
    operation: str = "fetch"

    def __post_init__(self) -> None:
        require_timezone_aware(self.fetched_at, field_name="fetched_at")


@dataclass(frozen=True)
class BronzeArtifact:
    """Raw source records captured by the Bronze stage."""

    source_key: str
    raw_records: tuple[dict[str, JsonValue], ...]
    fetch_params: dict[str, JsonValue] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=utc_now)
    provenance: ProvenanceEvent | None = None

    def __post_init__(self) -> None:
        require_timezone_aware(self.fetched_at, field_name="fetched_at")

    @property
    def record_count(self) -> int:
        """Return the number of preserved raw records."""
        return len(self.raw_records)
