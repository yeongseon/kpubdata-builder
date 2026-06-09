"""BuildSpec YAML 로딩·파싱 (Medallion 재구성: 기존 spec.py에서 분리).

이 모듈은 YAML 텍스트를 읽어 메모리 매핑으로 만든 뒤, models.py의 불변
데이터 클래스로 구조화한다. 타입/필수 키 검증 실패는 SpecLoadError로 변환한다.

주요 함수:
    - load_spec: YAML 파일 경로를 받아 BuildSpec으로 변환
    - parse_spec: 이미 로드된 매핑을 BuildSpec으로 파싱
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml

from ..errors import SpecLoadError
from .models import BuildSpec, ExportTarget, JsonValue, SourceRef, SplitSpec


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
        splits = _parse_splits(data.get("splits"))
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
        splits=splits,
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


def _validate_json_value(
    value: object, *, field_name: str, _ancestors: frozenset[int] = frozenset()
) -> JsonValue:
    """값이 JSON 프리미티브/컨테이너인지 재귀적으로 검증한다.

    YAML anchor/alias로 만들어진 순환 구조(예: ``a: &x {self: *x}``)를 만나면
    무한 재귀로 ``RecursionError`` crash가 발생할 수 있다. 현재 재귀 경로상의
    컨테이너 ``id()``를 추적해 순환을 감지하면 ``ValueError``로 명확히 실패한다
    (load_spec이 이를 SpecLoadError로 감싼다) (#169).
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, dict)):
        marker = id(value)
        if marker in _ancestors:
            raise ValueError(f"{field_name} contains a circular reference")
        child_ancestors = _ancestors | {marker}
        if isinstance(value, list):
            return [
                _validate_json_value(
                    item, field_name=f"{field_name}[{i}]", _ancestors=child_ancestors
                )
                for i, item in enumerate(value)
            ]
        result: dict[str, JsonValue] = {}
        for k, v in cast(dict[object, object], value).items():
            if not isinstance(k, str):
                raise TypeError(
                    f"{field_name} keys must be strings, got {type(k).__name__}"
                )
            result[k] = _validate_json_value(
                v, field_name=f"{field_name}.{k}", _ancestors=child_ancestors
            )
        return result
    raise TypeError(
        f"{field_name} contains non-JSON value of type {type(value).__name__}: {value!r}"
    )


def _parse_json_mapping(value: object, *, field_name: str) -> dict[str, JsonValue]:
    """JSON 호환 값만 담는 매핑 필드를 검증한다."""
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a mapping")

    raw_mapping = cast(dict[object, object], value)
    parsed: dict[str, JsonValue] = {}
    for key, item in raw_mapping.items():
        if not isinstance(key, str):
            raise TypeError(f"{field_name} keys must be strings")
        parsed[key] = _validate_json_value(item, field_name=f"{field_name}.{key}")
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


def _parse_splits(value: object) -> SplitSpec | None:
    """splits 매핑을 SplitSpec으로 변환한다(없으면 None)."""
    if value is None:
        return None
    mapping = _ensure_mapping(value, field_name="splits")
    mode = _require_string(mapping, "mode", prefix="splits")

    seed_obj = mapping.get("seed", 0)
    if not isinstance(seed_obj, int) or isinstance(seed_obj, bool):
        raise TypeError("splits.seed must be an integer")

    ratios: dict[str, float] = {}
    ratios_obj = mapping.get("ratios", {})
    if not isinstance(ratios_obj, dict):
        raise TypeError("splits.ratios must be a mapping")
    for name, fraction in cast(dict[object, object], ratios_obj).items():
        if not isinstance(name, str):
            raise TypeError("splits.ratios keys must be strings")
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            raise TypeError("splits.ratios values must be numbers")
        ratios[name] = float(fraction)

    key_obj = mapping.get("key", "")
    if not isinstance(key_obj, str):
        raise TypeError("splits.key must be a string")

    return SplitSpec(mode=mode, ratios=ratios, key=key_obj, seed=seed_obj)


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
    "load_spec",
    "parse_spec",
]
