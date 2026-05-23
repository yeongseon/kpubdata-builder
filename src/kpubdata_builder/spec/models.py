"""BuildSpec 데이터 모델 (Medallion 재구성: 기존 spec.py에서 분리).

이 모듈은 빌드 선언을 표현하는 불변 데이터 클래스와 JSON 호환 타입 별칭만
정의한다. YAML 로딩/파싱은 loader.py, 검증은 validator.py에 분리되어 있다.

주요 구성:
    - SourceRef: 원본 데이터 소스 참조
    - ExportTarget: 출력 대상 정의
    - BuildSpec: 전체 빌드 선언 모델
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True)
class SourceRef:
    """kpubdata의 정규화된 소스 쿼리를 가리키는 참조.

    속성:
        provider: provider 식별자.
        dataset: dataset 식별자.
        params: list 호출에 전달할 원시 파라미터.
        normalization_mode: canonical/raw 같은 정규화 모드.
        alias: 조립 단계에서 사용할 사용자 정의 소스 이름.
    """

    provider: str
    dataset: str
    params: dict[str, JsonValue] = field(default_factory=dict)
    normalization_mode: str = "canonical"
    alias: str = ""


@dataclass(frozen=True)
class ExportTarget:
    """빌드를 위한 구체적인 내보내기 대상 정의.

    속성:
        kind: exporter 레지스트리 키.
        output_path: output_dir 기준 상대 출력 경로.
        options: exporter 전용 선택 옵션.
    """

    kind: str
    output_path: str
    options: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class BuildSpec:
    """데이터셋 산출물을 위한 선언적 빌드 명세.

    속성:
        dataset_id: 데이터셋의 전역 식별자.
        title: 사람이 읽는 제목.
        description: 빌드 목적과 데이터 설명.
        sources: 입력 소스 목록.
        exports: 출력 대상 목록.
        transforms: 적용 예정인 변환 단계 이름 목록.
        metadata: 산출물에 실을 임의 메타데이터.
        publish: 빌드 후 게시까지 수행할지 여부.

    예시:
        >>> BuildSpec.from_yaml("specs/sample.yaml")
    """

    dataset_id: str
    title: str
    description: str
    sources: tuple[SourceRef, ...]
    exports: tuple[ExportTarget, ...]
    transforms: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)
    publish: bool = False

    @classmethod
    def from_yaml(cls, path: str | Path) -> BuildSpec:
        """YAML 파일에서 BuildSpec을 로드한다.

        매개변수:
            path: YAML 파일 경로.

        반환값:
            BuildSpec: 파싱 완료된 불변 명세 객체.
        """
        # models <-> loader 순환 import를 피하기 위한 지연 import.
        from .loader import load_spec

        return load_spec(Path(path))


__all__ = [
    "BuildSpec",
    "ExportTarget",
    "JsonPrimitive",
    "JsonValue",
    "SourceRef",
]
