"""빌드 매니페스트용 상세 provenance 모델 (#12).

이 모듈은 소스별 출처 추적 정보(언제·어디서·어떤 파라미터로 가져왔고, 몇 건을
받았으며, 데이터 체크섬은 무엇인지)를 담는 불변 값 객체와 빌더를 정의한다.
체크섬은 정렬 키 기반 JSON 직렬화 후 SHA-256으로 계산해 재현 가능하다.

주요 구성:
    - SourceProvenance: 단일 소스 fetch 출처 스냅샷
    - compute_data_checksum: 레코드의 재현 가능한 SHA-256 체크섬
    - build_source_provenance: 원시 입력 → SourceProvenance
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..spec import JsonValue


@dataclass(frozen=True)
class SourceProvenance:
    """단일 소스 fetch에 대한 상세 출처 정보.

    속성:
        provider: 데이터 제공자 식별자 (예: datago).
        dataset: 데이터셋 식별자.
        fetched_at: fetch 완료 시각 (UTC ISO 8601 문자열).
        record_count: 가져온 레코드 수.
        data_checksum: 데이터의 재현 가능한 체크섬 ("sha256:..." 형식).
        api_version: 소스 API 버전. 알 수 없으면 "unknown".
        params: fetch 요청 파라미터 스냅샷.
    """

    provider: str
    dataset: str
    fetched_at: str
    record_count: int
    data_checksum: str
    api_version: str = "unknown"
    params: dict[str, JsonValue] = field(default_factory=dict)


def compute_data_checksum(records: Sequence[Mapping[str, JsonValue]]) -> str:
    """레코드의 재현 가능한 SHA-256 체크섬을 계산한다.

    정렬 키 기반 JSON 직렬화로 키 순서 차이를 제거하므로, 동일 데이터는 항상
    동일 해시를 만든다.

    매개변수:
        records: 체크섬을 계산할 레코드 시퀀스.

    반환값:
        str: "sha256:" 접두사가 붙은 16진 해시.
    """
    payload = json.dumps(
        [dict(record) for record in records],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def build_source_provenance(
    *,
    provider: str,
    dataset: str,
    fetched_at: datetime,
    records: Sequence[Mapping[str, JsonValue]],
    params: Mapping[str, JsonValue],
    api_version: str = "unknown",
) -> SourceProvenance:
    """원시 fetch 정보로부터 SourceProvenance를 생성한다.

    매개변수:
        provider: 데이터 제공자 식별자.
        dataset: 데이터셋 식별자.
        fetched_at: fetch 완료 시각 (timezone-aware).
        records: 가져온 레코드 (수와 체크섬 계산에 사용).
        params: fetch 요청 파라미터.
        api_version: 소스 API 버전. 생략 시 "unknown".

    반환값:
        SourceProvenance: UTC ISO 시각과 체크섬이 채워진 출처 스냅샷.
    """
    return SourceProvenance(
        provider=provider,
        dataset=dataset,
        fetched_at=fetched_at.astimezone(timezone.utc).isoformat(),
        record_count=len(records),
        data_checksum=compute_data_checksum(records),
        api_version=api_version,
        params=dict(params),
    )


__all__ = ["SourceProvenance", "build_source_provenance", "compute_data_checksum"]
