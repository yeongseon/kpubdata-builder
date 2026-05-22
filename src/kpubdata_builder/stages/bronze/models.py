"""브론즈 단계 산출물과 출처 추적 모델."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ...spec import JsonValue


def utc_now() -> datetime:
    """시간대 정보가 있는 UTC 시각을 반환한다."""
    return datetime.now(tz=timezone.utc)


def require_timezone_aware(value: datetime, *, field_name: str) -> None:
    """datetime에 시간대 정보가 포함되어 있는지 검증한다."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True)
class ProvenanceEvent:
    """브론즈 소스 가져오기가 언제 어디서 일어났는지 기록한다."""

    source_key: str
    fetch_params: dict[str, JsonValue] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=utc_now)
    operation: str = "fetch"

    def __post_init__(self) -> None:
        require_timezone_aware(self.fetched_at, field_name="fetched_at")


@dataclass(frozen=True)
class BronzeArtifact:
    """브론즈 단계가 수집한 원시 소스 레코드."""

    source_key: str
    raw_records: tuple[dict[str, JsonValue], ...]
    fetch_params: dict[str, JsonValue] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=utc_now)
    provenance: ProvenanceEvent | None = None

    def __post_init__(self) -> None:
        require_timezone_aware(self.fetched_at, field_name="fetched_at")

    @property
    def record_count(self) -> int:
        """보존된 원시 레코드 수를 반환한다."""
        return len(self.raw_records)
