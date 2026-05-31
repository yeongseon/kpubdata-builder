"""Build specification models for orchestrating dataset builds."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias

import yaml

from .errors import SpecLoadError

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True)
class SourceRef:
    """Reference to a normalized source query from kpubdata."""

    provider: str
    dataset: str
    params: dict[str, JsonValue] = field(default_factory=dict)
    normalization_mode: str = "canonical"
    alias: str = ""


@dataclass(frozen=True)
class ExportTarget:
    """Concrete exporter target definition for a build."""

    kind: str
    output_path: str
    options: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class BuildSpec:
    """Declarative build specification for a dataset artifact."""

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
        return load_spec(Path(path))


def parse_spec(data: dict[str, object]) -> BuildSpec:
    try:
        dataset_id = _require_string(data, "dataset_id")
        title = _require_string(data, "title")
        description = _require_string(data, "description")
        transforms = _parse_string_list(data.get("transforms", []), field_name="transforms")
        metadata = _parse_string_dict(data.get("metadata", {}), field_name="metadata")
        publish = _parse_bool(data.get("publish", False), field_name="publish")
        sources = _parse_sources(data.get("sources", []))
        exports = _parse_exports(data.get("exports", []))
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
    try:
        raw_data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, yaml.YAMLError) as exc:
        raise SpecLoadError(f"Failed to load build spec from {path}: {exc}") from exc

    if not isinstance(raw_data, dict):
        raise SpecLoadError(
            f"Failed to parse build spec from {path}: top-level YAML must be a mapping"
        )

    return parse_spec(raw_data)


def _require_string(data: dict[str, object], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _parse_bool(value: object, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a boolean")
    return value


def _parse_string_list(value: object, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list")
    if not all(isinstance(item, str) for item in value):
        raise TypeError(f"{field_name} entries must be strings")
    return tuple(value)


def _parse_string_dict(value: object, *, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a mapping")
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise TypeError(f"{field_name} entries must be string pairs")
    return dict(value)


def _validate_json_value(value: object, *, field_name: str) -> JsonValue:
    """Recursively validate that a value conforms to the JsonValue type."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [
            _validate_json_value(item, field_name=f"{field_name}[{i}]")
            for i, item in enumerate(value)
        ]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(f"{field_name} keys must be strings, got {type(k).__name__}")
            result[k] = _validate_json_value(v, field_name=f"{field_name}.{k}")
        return result
    raise TypeError(
        f"{field_name} contains non-JSON value of type {type(value).__name__}: {value!r}"
    )


def _parse_json_mapping(value: object, *, field_name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise TypeError(f"{field_name} keys must be strings")
    return {
        key: _validate_json_value(item, field_name=f"{field_name}.{key}")
        for key, item in value.items()
    }


def _parse_sources(value: object) -> tuple[SourceRef, ...]:
    if not isinstance(value, list):
        raise TypeError("sources must be a list")

    parsed_sources: list[SourceRef] = []
    for index, item in enumerate(value):
        mapping = _ensure_mapping(item, field_name=f"sources[{index}]")
        provider = _require_string(mapping, "provider")
        dataset = _require_string(mapping, "dataset")
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
    if not isinstance(value, list):
        raise TypeError("exports must be a list")

    parsed_exports: list[ExportTarget] = []
    for index, item in enumerate(value):
        mapping = _ensure_mapping(item, field_name=f"exports[{index}]")
        kind = _require_string(mapping, "kind")
        output_path = _require_string(mapping, "output_path")
        options = _parse_json_mapping(
            mapping.get("options", {}), field_name=f"exports[{index}].options"
        )
        parsed_exports.append(ExportTarget(kind=kind, output_path=output_path, options=options))
    return tuple(parsed_exports)


def _ensure_mapping(value: object, *, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise TypeError(f"{field_name} keys must be strings")
    return {key: item for key, item in value.items()}


__all__ = [
    "BuildSpec",
    "ExportTarget",
    "JsonPrimitive",
    "JsonValue",
    "SourceRef",
    "load_spec",
    "parse_spec",
]
