"""브론즈 단계 산출물과 출처 추적 모델.

이 모듈은 원시 fetch 시각과 입력 파라미터를 포함한 provenance 정보,
그리고 실제 원시 레코드 묶음을 나타내는 데이터 클래스를 정의한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ...spec import JsonValue


def utc_now() -> datetime:
    """시간대 정보가 있는 UTC 시각을 반환한다.

    반환값:
        datetime: tzinfo가 설정된 UTC 현재 시각.
    """
    return datetime.now(tz=timezone.utc)


def require_timezone_aware(value: datetime, *, field_name: str) -> None:
    """datetime에 시간대 정보가 포함되어 있는지 검증한다.

    매개변수:
        value: 검사할 datetime 값.
        field_name: 오류 메시지에 사용할 필드 이름.

    예외:
        ValueError: naive datetime이 전달된 경우.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True)
class ProvenanceEvent:
    """브론즈 소스 가져오기가 언제 어디서 일어났는지 기록한다.

    속성:
        source_key: provider.dataset 형태 소스 식별자.
        fetch_params: 호출 파라미터 스냅샷.
        fetched_at: 실제 fetch 시각.
        operation: provenance 이벤트 종류.
    """

    source_key: str
    fetch_params: dict[str, JsonValue] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=utc_now)
    operation: str = "fetch"

    def __post_init__(self) -> None:
        """생성 직후 fetched_at의 timezone 유효성을 강제한다."""
        require_timezone_aware(self.fetched_at, field_name="fetched_at")


@dataclass(frozen=True)
class BronzeArtifact:
    """브론즈 단계가 수집한 원시 소스 레코드.

    속성:
        source_key: 어떤 소스에서 가져왔는지 나타내는 식별자.
        raw_records: 변환 전 원시 레코드 튜플.
        fetch_params: fetch 요청 시 사용한 파라미터.
        fetched_at: fetch 완료 시각.
        provenance: provenance 이벤트 스냅샷.
    """

    source_key: str
    raw_records: tuple[dict[str, JsonValue], ...]
    fetch_params: dict[str, JsonValue] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=utc_now)
    provenance: ProvenanceEvent | None = None

    def __post_init__(self) -> None:
        """생성 직후 fetched_at의 timezone 유효성을 강제한다."""
        require_timezone_aware(self.fetched_at, field_name="fetched_at")

    @property
    def record_count(self) -> int:
        """보존된 원시 레코드 수를 반환한다.

        반환값:
            int: raw_records 길이.
        """
        return len(self.raw_records)
