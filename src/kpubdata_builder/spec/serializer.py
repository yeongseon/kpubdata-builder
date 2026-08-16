"""Run별 재현성 감사를 위한 canonical BuildSpec 직렬화와 snapshot 저장."""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from pathlib import Path
from typing import cast

import yaml

from ..stages._path_safety import ensure_within, validate_path_segment
from .models import BuildSpec, JsonValue

BUILDSPEC_SNAPSHOT_FILENAME = "buildspec.yaml"
REDACTED_VALUE = "<redacted>"

# BuildSpec에는 별도 credential 모델이 없고 자유 형식 JSON 매핑만 있다. 따라서
# 부분 문자열 추측 대신, 실제로 credential 값에 쓰이는 명시적 키만 redaction한다.
_SECRET_FIELD_NAMES = frozenset(
    {
        "access_token",
        "accesstoken",
        "api_key",
        "apikey",
        "authorization",
        "bearer_token",
        "bearertoken",
        "client_secret",
        "clientsecret",
        "password",
        "refresh_token",
        "refreshtoken",
        "secret",
        "service_key",
        "servicekey",
        "token",
        # 이 저장소가 실제 credential 환경변수/헤더 이름으로 사용하는 키.
        "hf_token",
        "kaggle_key",
        "kpubdata_builder_api_key",
        "kpubdata_datago_api_key",
        "x_api_key",
    }
)


def _normalized_key(key: str) -> str:
    return key.casefold().replace("-", "_")


def _canonical_json(value: JsonValue) -> JsonValue:
    """JSON 값을 key 순서와 secret redaction이 고정된 값으로 복사한다."""
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key in sorted(value):
            item = value[key]
            result[key] = (
                REDACTED_VALUE
                if _normalized_key(key) in _SECRET_FIELD_NAMES
                else _canonical_json(item)
            )
        return result
    if isinstance(value, list):
        return [_canonical_json(item) for item in value]
    return value


def canonical_spec_mapping(spec: BuildSpec) -> dict[str, JsonValue]:
    """BuildSpec을 필드 순서와 optional 기본값이 고정된 JSON 호환 매핑으로 만든다."""
    sources: list[JsonValue] = []
    for source in spec.sources:
        schema: JsonValue = None
        if source.schema is not None:
            schema = {
                "required": list(source.schema.required),
                "dtypes": _canonical_json(cast(JsonValue, source.schema.dtypes)),
                "casts": _canonical_json(cast(JsonValue, source.schema.casts)),
            }
        # kind별로 유효한 field만 싣는다 (#498). loader의 _reject_foreign_fields가
        # kind-foreign field의 "존재"만으로 거부하므로, 여기서 모든 kind의 field를
        # 항상 함께 실으면 canonical snapshot 자체가 round-trip 불가능한 spec이
        # 된다 — schema(#437)가 이미 쓰는 "관련 없으면 아예 emit하지 않는다" 패턴을
        # 그대로 따른다. 기존 public_api-only spec에는 "kind": "public_api" 한
        # 필드만 additive로 늘어난다.
        entry: dict[str, JsonValue] = {"kind": source.kind, "alias": source.alias, "schema": schema}
        if source.kind == "file":
            entry["upload_id"] = source.upload_id
            entry["format"] = source.format
            entry["encoding"] = source.encoding
        elif source.kind == "url":
            entry["endpoint"] = source.endpoint
            entry["method"] = source.method
            entry["format"] = source.format
        else:
            entry["provider"] = source.provider
            entry["dataset"] = source.dataset
            entry["params"] = _canonical_json(source.params)
        sources.append(entry)

    exports: list[JsonValue] = [
        {
            "kind": target.kind,
            "output_path": target.output_path,
            "options": _canonical_json(target.options),
        }
        for target in spec.exports
    ]

    splits: JsonValue = None
    if spec.splits is not None:
        splits = {
            "mode": spec.splits.mode,
            "ratios": _canonical_json(cast(JsonValue, spec.splits.ratios)),
            "key": spec.splits.key,
            "seed": spec.splits.seed,
        }

    pii: JsonValue = None
    if spec.pii is not None:
        pii = {"mode": spec.pii.mode, "allow_columns": list(spec.pii.allow_columns)}

    quality: JsonValue = None
    if spec.quality is not None:
        quality = {
            "max_duplicate_rate": spec.quality.max_duplicate_rate,
            "max_duplicate_rate_severity": spec.quality.max_duplicate_rate_severity,
            "max_null_ratio": _canonical_json(cast(JsonValue, spec.quality.max_null_ratio)),
            "max_null_ratio_severity": _canonical_json(
                cast(JsonValue, spec.quality.max_null_ratio_severity)
            ),
            "min_rows": spec.quality.min_rows,
            "min_rows_severity": spec.quality.min_rows_severity,
            "range": [
                {"column": r.column, "min": r.min, "max": r.max, "severity": r.severity}
                for r in spec.quality.range
            ],
            "compare_columns": [
                {"left": r.left, "operator": r.operator, "right": r.right, "severity": r.severity}
                for r in spec.quality.compare_columns
            ],
        }

    return {
        "dataset_id": spec.dataset_id,
        "title": spec.title,
        "description": spec.description,
        "sources": sources,
        "exports": exports,
        "metadata": _canonical_json(spec.metadata),
        "publish": spec.publish,
        "splits": splits,
        "pii": pii,
        "license": spec.license,
        "quality": quality,
    }


def serialize_spec(spec: BuildSpec) -> str:
    """BuildSpec을 결정적인 canonical UTF-8 YAML 문자열로 직렬화한다."""
    return yaml.safe_dump(
        canonical_spec_mapping(spec),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=4096,
    )


def serialize_spec_bytes(spec: BuildSpec) -> bytes:
    """실제 snapshot에 기록할 canonical bytes를 반환한다."""
    return serialize_spec(spec).encode("utf-8")


def compute_spec_digest(payload: bytes) -> str:
    """canonical snapshot bytes의 SHA-256 digest를 반환한다."""
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def write_buildspec_snapshot(
    spec: BuildSpec, *, output_root: Path, run_id: str
) -> tuple[Path, str]:
    """canonical BuildSpec을 run workspace에 원자적으로 기록하고 digest를 반환한다."""
    validate_path_segment(run_id, field_name="run_id")
    run_dir = output_root / run_id
    ensure_within(output_root, run_dir, label="run directory")
    snapshot_path = run_dir / BUILDSPEC_SNAPSHOT_FILENAME
    ensure_within(run_dir, snapshot_path, label="BuildSpec snapshot")
    payload = serialize_spec_bytes(spec)

    run_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=run_dir, prefix=".buildspec_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, snapshot_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return snapshot_path, compute_spec_digest(payload)


__all__ = [
    "BUILDSPEC_SNAPSHOT_FILENAME",
    "REDACTED_VALUE",
    "canonical_spec_mapping",
    "compute_spec_digest",
    "serialize_spec",
    "serialize_spec_bytes",
    "write_buildspec_snapshot",
]
