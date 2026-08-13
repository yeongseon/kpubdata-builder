"""Run별 canonical BuildSpec snapshot 계약 검증 (#487)."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
import yaml

import kpubdata_builder.spec.serializer as serializer
from kpubdata_builder.spec import (
    BuildSpec,
    ExportTarget,
    SourceRef,
    compute_spec_digest,
    parse_spec,
    serialize_spec,
    serialize_spec_bytes,
    write_buildspec_snapshot,
)
from kpubdata_builder.spec.models import PiiPolicy, QualityPolicy, SchemaContract, SplitSpec


def _complete_spec() -> BuildSpec:
    return BuildSpec(
        dataset_id="dataset.contract",
        title="계약 데이터",
        description="모든 optional field",
        sources=(
            SourceRef(
                provider="datago",
                dataset="air_quality",
                params={"nested": {"z": 1, "a": [True, None]}, "page": 1},
                alias="air",
                schema=SchemaContract(
                    required=("id",), dtypes={"id": "string"}, casts={"value": "float64"}
                ),
            ),
        ),
        exports=(
            ExportTarget(
                kind="jsonl",
                output_path="out/data.jsonl",
                options={"indent": 2, "flags": ["a", "b"]},
            ),
        ),
        metadata={"coverage": {"year": 2026, "note": None}, "public": True},
        publish=False,
        splits=SplitSpec(mode="ratio", ratios={"train": 0.8, "test": 0.2}, seed=7),
        pii=PiiPolicy(mode="warn", allow_columns=("contact_hint",)),
        license="CC-BY-4.0",
        quality=QualityPolicy(
            max_duplicate_rate=0.01, max_null_ratio={"value": 0.05}, min_rows=100
        ),
    )


def _parse_yaml(text: str) -> BuildSpec:
    raw = yaml.safe_load(text)
    assert isinstance(raw, dict)
    return parse_spec(cast(dict[str, object], raw))


def test_minimal_and_complete_spec_round_trip() -> None:
    minimal = BuildSpec(
        dataset_id="d",
        title="t",
        description="desc",
        sources=(SourceRef(provider="p", dataset="s"),),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
    )

    assert _parse_yaml(serialize_spec(minimal)) == minimal
    assert _parse_yaml(serialize_spec(_complete_spec())) == _complete_spec()


def test_serialization_is_deterministic_for_nested_mapping_order() -> None:
    first = _complete_spec()
    second = replace(first, metadata={"public": True, "coverage": {"note": None, "year": 2026}})

    assert first == second
    assert serialize_spec_bytes(first) == serialize_spec_bytes(second)
    assert compute_spec_digest(serialize_spec_bytes(first)) == compute_spec_digest(
        serialize_spec_bytes(second)
    )


def test_secret_redaction_is_exact_and_recursive() -> None:
    spec = _complete_spec()
    source = spec.sources[0]
    redacted = replace(
        spec,
        sources=(
            replace(
                source,
                params={
                    "api_key": "plain-api-secret",
                    "apiKey": "plain-camel-api-secret",
                    "service_key": "plain-service-secret",
                    "nested": [
                        {
                            "serviceKey": "plain-camel-service-secret",
                            "access_token": "plain-access-token",
                        },
                        {"refresh_token": "plain-refresh-token"},
                    ],
                    "monkey": "keep-me",
                    "token_count": 3,
                },
            ),
        ),
        exports=(
            replace(
                spec.exports[0],
                options={
                    "client_secret": "plain-client-secret",
                    "Authorization": "plain-authorization",
                },
            ),
        ),
        metadata={
            "password": "plain-password",
            "HF_TOKEN": "plain-hf-token",
            "KAGGLE_KEY": "plain-kaggle-key",
            "KPUBDATA_DATAGO_API_KEY": "plain-datago-key",
            "KPUBDATA_BUILDER_API_KEY": "plain-builder-key",
            "bearer_token": "plain-bearer-token",
            "key": "business-key",
        },
    )

    text = serialize_spec(redacted)

    assert "plain-api-secret" not in text
    plaintext_secrets = (
        "plain-api-secret",
        "plain-camel-api-secret",
        "plain-service-secret",
        "plain-camel-service-secret",
        "plain-access-token",
        "plain-refresh-token",
        "plain-client-secret",
        "plain-authorization",
        "plain-password",
        "plain-hf-token",
        "plain-kaggle-key",
        "plain-datago-key",
        "plain-builder-key",
        "plain-bearer-token",
    )
    assert all(secret not in text for secret in plaintext_secrets)
    assert text.count("<redacted>") == len(plaintext_secrets)
    assert "keep-me" in text
    assert "business-key" in text
    assert "token_count: 3" in text
    # credential-bearing snapshot은 의도적으로 원본과 동일한 round-trip을 제공하지 않는다.
    assert _parse_yaml(text) != redacted


def test_specs_differing_only_by_secret_share_redacted_snapshot_digest() -> None:
    first = replace(
        _complete_spec(),
        metadata={"api_key": "first-secret", "label": "same-public-value"},
    )
    second = replace(
        first,
        metadata={"api_key": "second-secret", "label": "same-public-value"},
    )

    first_payload = serialize_spec_bytes(first)
    second_payload = serialize_spec_bytes(second)

    assert first_payload == second_payload
    assert compute_spec_digest(first_payload) == compute_spec_digest(second_payload)


def test_snapshot_bytes_and_digest_are_identical(tmp_path: Path) -> None:
    path, digest = write_buildspec_snapshot(_complete_spec(), output_root=tmp_path, run_id="r1")
    payload = path.read_bytes()

    assert path == tmp_path / "r1" / "buildspec.yaml"
    assert payload == serialize_spec_bytes(_complete_spec())
    assert digest == f"sha256:{hashlib.sha256(payload).hexdigest()}"
    changed_payload = serialize_spec_bytes(replace(_complete_spec(), title="변경된 제목"))
    assert compute_spec_digest(changed_payload) != digest


def test_snapshot_write_is_atomic_and_cleans_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_replace(_source: str, _destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(serializer.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        write_buildspec_snapshot(_complete_spec(), output_root=tmp_path, run_id="r1")

    assert not (tmp_path / "r1" / "buildspec.yaml").exists()
    assert list((tmp_path / "r1").glob(".buildspec_*.tmp")) == []


def test_snapshot_rejects_unsafe_run_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="run_id"):
        write_buildspec_snapshot(_complete_spec(), output_root=tmp_path, run_id="../escape")
