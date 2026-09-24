"""브론즈 단계 소스 가져오기 도우미.

이 모듈은 kpubdata 호환 클라이언트에서 원시 레코드를 가져와
BronzeArtifact와 provenance 정보를 구성하는 최소 fetch 계층을 제공한다.

주요 구성:
    - DatasetResult / SourceDataset / SourceClient: 필요한 최소 Protocol 계약
    - build_bronze_artifact: 원시 fetch 결과를 브론즈 산출물로 변환
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol, cast, runtime_checkable

from ...spec import JsonValue
from .models import BronzeArtifact, ProvenanceEvent, require_timezone_aware, utc_now


class DatasetResult(Protocol):
    """호환되는 kpubdata 데이터셋이 반환하는 최소 결과 형태.

    속성:
        items: fetch된 원시 레코드 iterable.
    """

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        """가져온 레코드를 반환한다."""
        ...


class SourceDataset(Protocol):
    """브론즈 단계에서 사용하는 최소 데이터셋 형태."""

    def list(self, **params: JsonValue) -> DatasetResult:
        """하나의 파라미터 집합에 대해 레코드를 가져온다."""
        ...


@runtime_checkable
class PaginatedSourceDataset(SourceDataset, Protocol):
    """kpubdata Dataset.list_all() pagination 계약."""

    def list_all(self, **params: JsonValue) -> Iterable[DatasetResult]: ...


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
    param_combinations: Sequence[dict[str, JsonValue]] | None = None,
) -> BronzeArtifact:
    """호환 클라이언트에서 원시 레코드를 가져와 브론즈 산출물을 반환한다.

    매개변수:
        client: dataset(source_key)를 제공하는 클라이언트.
        source_key: provider.dataset 형태의 소스 식별자.
        fetch_params: dataset.list 호출에 전달할 파라미터. ``param_combinations``
            가 있으면 이 값은 호출에 쓰이지 않고 provenance 에만 남는다 —
            공통 파라미터는 이미 각 조합에 병합되어 있기 때문이다.
        param_combinations: 여러 호출 조합 (#613). 주어지면 조합마다 한 번씩
            호출하고 결과를 **선언된 순서 그대로** 이어붙여 하나의 artifact 로
            만든다. 순서는 계약이다 — 바뀌면 artifact_id 가 바뀐다.
        fetched_at: fetch 완료 시각. 생략 시 현재 UTC 시각 사용.

    반환값:
        BronzeArtifact: 원시 레코드와 provenance를 담은 산출물.

    예외:
        ValueError: fetched_at에 timezone 정보가 없을 때.
    """
    resolved_params = dict(fetch_params or {})
    resolved_fetched_at = fetched_at or utc_now()
    require_timezone_aware(resolved_fetched_at, field_name="fetched_at")

    combinations = tuple(param_combinations) if param_combinations is not None else None
    if combinations is not None and not combinations:
        # 빈 전개는 호출을 한 번도 하지 않고 빈 Bronze 를 성공으로 만든다.
        # validator 가 선언 시점에 막지만, 라이브러리 직접 호출 경로도 막는다.
        raise ValueError("param_combinations must not be empty")
    calls = combinations if combinations is not None else (resolved_params,)

    dataset = client.dataset(source_key)
    records: list[dict[str, JsonValue]] = []
    for call_params in calls:
        # 조합 순서대로 이어붙인다. 순서가 바뀌면 raw_records.jsonl 의 바이트가
        # 바뀌고 artifact_id 가 따라 바뀐다 — R1 의 재빌드 결정성이 그 위에 있다.
        if isinstance(dataset, PaginatedSourceDataset):
            records.extend(
                record for batch in dataset.list_all(**call_params) for record in batch.items
            )
        else:
            records.extend(dataset.list(**call_params).items)
    raw_records = tuple(records)

    # 전개된 조합 전체를 provenance 에 남긴다. 어떤 조합으로 만든 Bronze 인지가
    # 남지 않으면 재현성 실험이 근거를 잃는다 (#613). 단일 호출이면 기존과 같은
    # 모양을 유지한다 — 쓰지 않는 기능이 provenance 를 바꾸지 않게 한다.
    provenance_params: dict[str, JsonValue] = dict(resolved_params)
    if combinations is not None:
        provenance_params = {
            **resolved_params,
            "param_combinations": cast(JsonValue, [dict(c) for c in combinations]),
        }
    provenance = ProvenanceEvent(
        source_key=source_key,
        fetch_params=provenance_params,
        fetched_at=resolved_fetched_at,
    )

    return BronzeArtifact(
        source_key=source_key,
        raw_records=raw_records,
        fetch_params=provenance_params,
        fetched_at=resolved_fetched_at,
        provenance=provenance,
    )
