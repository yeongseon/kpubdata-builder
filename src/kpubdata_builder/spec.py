"""데이터셋 빌드를 오케스트레이션하기 위한 빌드 명세 모델.

이 모듈은 YAML 기반 BuildSpec을 로드하고, 파이썬 데이터 구조로 파싱하며,
후속 검증 단계가 사용할 불변 데이터 클래스를 제공한다.

주요 구성:
    - SourceRef: 원본 데이터 소스 참조
    - ExportTarget: 출력 대상 정의
    - BuildSpec: 전체 빌드 선언 모델
    - load_spec / parse_spec: YAML 로드 및 구조 파싱 진입점
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias, cast

import yaml

from .errors import SpecLoadError

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
        return load_spec(Path(path))


def parse_spec(data: dict[str, object]) -> BuildSpec:
    """메모리 상의 매핑 데이터를 BuildSpec으로 파싱한다.

    매개변수:
        data: YAML 로더가 반환한 최상위 매핑.

    반환값:
        BuildSpec: 검증 가능한 빌드 명세 객체.

    예외:
        SpecLoadError: 필드 타입이 맞지 않거나 필수 키가 없을 때.
    """
    try:
        dataset_id = _require_string(data, "dataset_id")
        title = _require_string(data, "title")
        description = _require_string(data, "description")
        transforms = _parse_string_list(data.get("transforms", []), field_name="transforms")
        metadata = _parse_string_dict(data.get("metadata", {}), field_name="metadata")
        publish = _parse_bool(data.get("publish", False), field_name="publish")
        sources = _parse_sources(_require_present(data, "sources"))
        exports = _parse_exports(_require_present(data, "exports"))
    except (KeyError, TypeError, ValueError) as exc:
        raise SpecLoadError(f"Failed to parse build spec: {exc}") from exc

    return BuildSpec(
        dataset_id=dataset_id,
        title=title,
        description=description,
        sources=sources,
        exports=exports,
        transforms=transforms,
        metadata=metadata,
        publish=publish,
    )


def load_spec(path: Path) -> BuildSpec:
    """YAML 파일을 읽어 BuildSpec으로 변환한다.

    매개변수:
        path: BuildSpec YAML 경로.

    반환값:
        BuildSpec: 로드된 명세 객체.

    예외:
        SpecLoadError: 파일 읽기, YAML 파싱, 최상위 구조 검증 실패 시.
    """
    try:
        raw_data = cast(object, yaml.safe_load(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, OSError, yaml.YAMLError) as exc:
        raise SpecLoadError(f"Failed to load build spec from {path}: {exc}") from exc

    if not isinstance(raw_data, dict):
        raise SpecLoadError(
            f"Failed to parse build spec from {path}: top-level YAML must be a mapping"
        )

    return parse_spec(cast(dict[str, object], raw_data))


def _require_string(data: dict[str, object], key: str, *, prefix: str = "") -> str:
    """필수 문자열 필드를 추출한다."""
    label = f"{prefix}.{key}" if prefix else key
    if key not in data:
        raise KeyError(f"{label} is required")
    value = data[key]
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not value:
        raise ValueError(f"{label} must not be empty")
    return value


def _require_present(data: dict[str, object], key: str) -> object:
    if key not in data:
        raise KeyError(f"{key} is required")
    return data[key]


def _parse_bool(value: object, *, field_name: str) -> bool:
    """불리언 필드 타입을 검증한다."""
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a boolean")
    return value


def _parse_string_list(value: object, *, field_name: str) -> tuple[str, ...]:
    """문자열 목록 필드를 불변 튜플로 변환한다."""
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list")

    items = cast(list[object], value)
    if not all(isinstance(item, str) for item in items):
        raise TypeError(f"{field_name} entries must be strings")
    return tuple(cast(str, item) for item in items)


def _parse_string_dict(value: object, *, field_name: str) -> dict[str, str]:
    """문자열 키/값 매핑을 검증하고 새 dict로 복사한다."""
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a mapping")

    raw_mapping = cast(dict[object, object], value)
    parsed: dict[str, str] = {}
    for key, item in raw_mapping.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise TypeError(f"{field_name} entries must be string pairs")
        parsed[key] = item
    return parsed


def _parse_json_mapping(value: object, *, field_name: str) -> dict[str, JsonValue]:
    """JSON 호환 값만 담는 매핑 필드를 검증한다."""
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a mapping")

    raw_mapping = cast(dict[object, JsonValue], value)
    parsed: dict[str, JsonValue] = {}
    for key, item in raw_mapping.items():
        if not isinstance(key, str):
            raise TypeError(f"{field_name} keys must be strings")
        parsed[key] = item
    return parsed


def _parse_sources(value: object) -> tuple[SourceRef, ...]:
    """sources 배열을 SourceRef 튜플로 변환한다."""
    if not isinstance(value, list):
        raise TypeError("sources must be a list")
    if not value:
        raise ValueError("sources must not be empty")

    items = cast(list[object], value)
    parsed_sources: list[SourceRef] = []
    for index, item in enumerate(items):
        prefix = f"sources[{index}]"
        mapping = _ensure_mapping(item, field_name=prefix)
        provider = _require_string(mapping, "provider", prefix=prefix)
        dataset = _require_string(mapping, "dataset", prefix=prefix)
        params = _parse_json_mapping(
            mapping.get("params", {}), field_name=f"sources[{index}].params"
        )
        normalization_mode_obj = mapping.get("normalization_mode", "canonical")
        alias_obj = mapping.get("alias", "")
        if not isinstance(normalization_mode_obj, str):
            raise TypeError(f"sources[{index}].normalization_mode must be a string")
        if not isinstance(alias_obj, str):
            raise TypeError(f"sources[{index}].alias must be a string")
        parsed_sources.append(
            SourceRef(
                provider=provider,
                dataset=dataset,
                params=params,
                normalization_mode=normalization_mode_obj,
                alias=alias_obj,
            )
        )
    return tuple(parsed_sources)


def _parse_exports(value: object) -> tuple[ExportTarget, ...]:
    """exports 배열을 ExportTarget 튜플로 변환한다."""
    if not isinstance(value, list):
        raise TypeError("exports must be a list")
    if not value:
        raise ValueError("exports must not be empty")

    items = cast(list[object], value)
    parsed_exports: list[ExportTarget] = []
    for index, item in enumerate(items):
        prefix = f"exports[{index}]"
        mapping = _ensure_mapping(item, field_name=prefix)
        kind = _require_string(mapping, "kind", prefix=prefix)
        output_path = _require_string(mapping, "output_path", prefix=prefix)
        options = _parse_json_mapping(
            mapping.get("options", {}), field_name=f"exports[{index}].options"
        )
        parsed_exports.append(ExportTarget(kind=kind, output_path=output_path, options=options))
    return tuple(parsed_exports)


def _ensure_mapping(value: object, *, field_name: str) -> dict[str, object]:
    """문자열 키를 가진 매핑인지 확인하고 복사본을 반환한다."""
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a mapping")

    raw_mapping = cast(dict[object, object], value)
    parsed: dict[str, object] = {}
    for key, item in raw_mapping.items():
        if not isinstance(key, str):
            raise TypeError(f"{field_name} keys must be strings")
        parsed[key] = item
    return parsed


__all__ = [
    "BuildSpec",
    "ExportTarget",
    "JsonPrimitive",
    "JsonValue",
    "SourceRef",
    "load_spec",
    "parse_spec",
]
