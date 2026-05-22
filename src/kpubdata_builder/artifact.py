"""내보내기 도구가 사용하는 표준 조립 산출물 모델."""

from __future__ import annotations

from dataclasses import dataclass, field

from .spec import JsonValue


@dataclass(frozen=True)
class ArtifactDataset:
    """구체적인 내보내기 전에 사용하는 조립된 데이터셋 표현."""

    records: tuple[dict[str, JsonValue], ...] = ()
    schema: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)
    provenance: tuple[str, ...] = ()
    statistics: dict[str, int] = field(default_factory=dict)
