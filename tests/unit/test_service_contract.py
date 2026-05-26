"""Builder Service Contract(#63) OpenAPI 스펙의 구조 검증.

설치된 OpenAPI validator가 없으므로, 계약이 OpenAPI 3.1이고 API_CONTRACT.md의
8개 엔드포인트와 표준 오류 스키마를 모두 담는지 구조적으로 검증한다. 실행 중인
builder dev server 대비 계약 테스트는 service mode(#36) 도입 후 확장한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

_CONTRACT_PATH = Path(__file__).parents[2] / "contract" / "builder-api.yaml"

# (path, method) 형태의 계약 필수 오퍼레이션.
_REQUIRED_OPERATIONS = [
    ("/datasets", "get"),
    ("/spec/validate", "post"),
    ("/preview", "post"),
    ("/builds", "post"),
    ("/builds/{id}", "get"),
    ("/builds/{id}/manifest", "get"),
    ("/builds/{id}/artifacts", "get"),
    ("/publish", "post"),
]


def _load_contract() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8")))


def test_contract_file_exists() -> None:
    assert _CONTRACT_PATH.is_file()


def test_is_openapi_3_1_with_info() -> None:
    contract = _load_contract()

    assert str(contract["openapi"]).startswith("3.1")
    assert contract["info"]["title"]
    assert contract["info"]["version"]


def test_covers_all_required_operations() -> None:
    paths = _load_contract()["paths"]

    for path, method in _REQUIRED_OPERATIONS:
        assert path in paths, f"missing path: {path}"
        assert method in paths[path], f"missing {method.upper()} {path}"
        assert paths[path][method].get("operationId"), f"missing operationId for {method} {path}"


def test_operation_ids_are_unique() -> None:
    paths = _load_contract()["paths"]
    operation_ids = [
        operation["operationId"]
        for methods in paths.values()
        for operation in methods.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]

    assert len(operation_ids) == len(set(operation_ids))


def test_defines_standard_error_schema() -> None:
    schemas = _load_contract()["components"]["schemas"]

    assert "Error" in schemas
    error_props = schemas["Error"]["properties"]["error"]["properties"]
    assert {"code", "message", "details"} <= set(error_props)


def test_build_state_enum_matches_contract() -> None:
    schemas = _load_contract()["components"]["schemas"]

    assert set(schemas["BuildState"]["enum"]) == {
        "draft",
        "validated",
        "running",
        "exported",
        "manifested",
        "published",
        "failed",
    }


def test_referenced_schemas_resolve() -> None:
    # 모든 로컬 $ref("#/components/...")가 실제로 존재하는지 확인한다.
    contract = _load_contract()

    def _iter_refs(node: object) -> list[str]:
        refs: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str):
                    refs.append(value)
                else:
                    refs.extend(_iter_refs(value))
        elif isinstance(node, list):
            for item in node:
                refs.extend(_iter_refs(item))
        return refs

    for ref in _iter_refs(contract):
        assert ref.startswith("#/"), f"unexpected external ref: {ref}"
        target: Any = contract
        for part in ref.lstrip("#/").split("/"):
            assert part in target, f"unresolved $ref: {ref}"
            target = target[part]
