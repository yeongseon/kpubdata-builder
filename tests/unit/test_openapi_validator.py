"""``tests/unit/_openapi.py`` 순수 파이썬 validator의 단위 테스트 (#209, ADR-0005).

validator 자체가 드리프트를 잡아내는지(필수 필드 누락·타입 변경) 증명한다.
conformance 게이트의 가치는 이 검증기가 "유효 응답은 통과시키고, 회귀 응답은
실패시키는지"에 달려 있으므로, 합격/불합격 경계를 명시적으로 고정한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from ._openapi import resolve_ref, response_schema, validate

_CONTRACT_PATH = Path(__file__).parents[2] / "contract" / "builder-api.yaml"


def _contract() -> dict[str, Any]:
    return yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))


# --- type / required / enum / minimum 경계 -------------------------------------


def test_accepts_object_with_all_required_fields() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "required": ["status", "api_version"],
        "properties": {"status": {"type": "string"}, "api_version": {"type": "string"}},
    }
    assert validate({"status": "valid", "api_version": "1.0.0"}, schema, {}) == []


def test_rejects_missing_required_field() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "required": ["status", "api_version"],
        "properties": {"status": {"type": "string"}, "api_version": {"type": "string"}},
    }
    errors = validate({"status": "valid"}, schema, {})
    assert any("api_version" in e for e in errors)


def test_rejects_wrong_value_type() -> None:
    schema: dict[str, Any] = {"type": "object", "properties": {"n": {"type": "integer"}}}
    errors = validate({"n": "not-an-int"}, schema, {})
    assert errors and any("integer" in e for e in errors)


def test_integer_does_not_accept_bool() -> None:
    # bool은 int의 하위 타입이지만 JSON integer가 아니다.
    schema: dict[str, Any] = {"type": "integer"}
    assert validate(True, schema, {})  # True/False는 integer로 거부


def test_nullable_union_accepts_string_and_null() -> None:
    schema: dict[str, Any] = {"type": ["string", "null"]}
    assert validate(None, schema, {}) == []
    assert validate("ok", schema, {}) == []
    assert validate(7, schema, {})  # 정수는 거부


def test_enum_rejects_out_of_range_value() -> None:
    schema: dict[str, Any] = {"type": "string", "enum": ["ok", "failed"]}
    assert validate("ok", schema, {}) == []
    assert validate("pending", schema, {})  # enum 밖


def test_minimum_rejects_below_bound() -> None:
    schema: dict[str, Any] = {"type": "integer", "minimum": 1}
    assert validate(1, schema, {}) == []
    assert validate(0, schema, {})


# --- 구조적 키워드: $ref / oneOf / additionalProperties / items -----------------


def test_ref_resolves_local_schema() -> None:
    contract: dict[str, Any] = {
        "components": {"schemas": {"Foo": {"type": "object", "required": ["x"]}}}
    }
    assert validate({"x": 1}, {"$ref": "#/components/schemas/Foo"}, contract) == []
    assert validate({}, {"$ref": "#/components/schemas/Foo"}, contract)  # x 누락


def test_ref_raises_on_unresolved_target() -> None:
    with pytest.raises(ValueError):
        resolve_ref({"components": {"schemas": {}}}, "#/components/schemas/Missing")


def test_oneof_accepts_when_at_least_one_branch_matches() -> None:
    contract: dict[str, Any] = {
        "components": {
            "schemas": {
                "Error": {"type": "object", "required": ["error"]},
                "ValidationError": {"type": "object", "required": ["status"]},
            }
        }
    }
    schema: dict[str, Any] = {
        "oneOf": [
            {"$ref": "#/components/schemas/Error"},
            {"$ref": "#/components/schemas/ValidationError"},
        ]
    }
    # Error 형태와 ValidationError 형태 각각이 한 분기에 맞는다.
    assert validate({"error": "boom"}, schema, contract) == []
    assert validate({"status": "invalid"}, schema, contract) == []
    # 어느 쪽도 아니면 실패.
    assert validate({"unexpected": 1}, schema, contract)


def test_additional_properties_schema_validates_extras() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"known": {"type": "string"}},
        "additionalProperties": {"type": "string"},
    }
    assert validate({"known": "a", "extra": "b"}, schema, {}) == []
    # 추가 프로퍼티가 문자열이 아니면 거부.
    assert validate({"known": "a", "extra": 5}, schema, {})


def test_additional_properties_false_rejects_extras() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"known": {"type": "string"}},
        "additionalProperties": False,
    }
    assert validate({"known": "a"}, schema, {}) == []
    assert validate({"known": "a", "extra": "b"}, schema, {})


def test_additional_properties_omitted_allows_extras() -> None:
    # 응답에 새 선택 필드가 더해지는 부가적 변화는 통과시킨다(전방 호환).
    schema: dict[str, Any] = {
        "type": "object",
        "required": ["status"],
        "properties": {"status": {"type": "string"}},
    }
    assert validate({"status": "ok", "future_field": 123}, schema, {}) == []


def test_array_items_are_each_validated() -> None:
    schema: dict[str, Any] = {"type": "array", "items": {"type": "integer"}}
    assert validate([1, 2, 3], schema, {}) == []
    errors = validate([1, "x", 3], schema, {})
    assert any("$[1]" in e for e in errors)


# --- response_schema 조회 / 경로 정규화 ----------------------------------------


def test_response_schema_returns_declared_schema() -> None:
    contract = _contract()
    schema = response_schema(contract, "/version", "GET", 200)
    assert schema is not None
    assert validate({"service": "kpubdata-builder", "api_version": "1.0.0"}, schema, contract) == []


def test_response_schema_none_for_undeclared_status() -> None:
    contract = _contract()
    assert response_schema(contract, "/version", "GET", 500) is None


def test_response_schema_normalizes_path_template() -> None:
    # /artifacts/{run_id} 템플릿을 구체적 run_id로 조회한다.
    contract = _contract()
    assert response_schema(contract, "/artifacts/any-run-id", "GET", 200) is not None
    assert response_schema(contract, "/artifacts/any-run-id", "GET", 404) is not None


# --- 게이트 가치 증명: 실제 계약 스키마로 회귀 잡기 ----------------------------


def test_gate_catches_required_field_drift() -> None:
    """app.py가 BuildSuccessResponse에서 run_id를 빼먹는 회귀를 게이트가 잡는다."""
    contract = _contract()
    schema = response_schema(contract, "/build", "POST", 200)
    assert schema is not None
    good = {
        "status": "ok",
        "run_id": "r1",
        "outcomes": [],
        "manifest": "/p/manifest.json",
        "api_version": "1.0.0",
    }
    assert validate(good, schema, contract) == []
    drifted = dict(good)
    del drifted["run_id"]
    assert validate(drifted, schema, contract)


def test_gate_catches_type_drift() -> None:
    """api_version이 문자열에서 정수로 바뀌는 회귀도 잡는다."""
    contract = _contract()
    schema = response_schema(contract, "/version", "GET", 200)
    assert schema is not None
    assert validate({"service": "kpubdata-builder", "api_version": 1}, schema, contract)
