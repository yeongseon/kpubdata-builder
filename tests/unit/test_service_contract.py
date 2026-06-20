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


def test_service_api_version_matches_contract() -> None:
    # 코드의 API_CONTRACT_VERSION이 계약 문서의 info.version과 어긋나지 않도록 고정 (#209).
    from kpubdata_builder.service import API_CONTRACT_VERSION

    assert str(_load_contract()["info"]["version"]) == API_CONTRACT_VERSION


# 현재 BuilderService가 구현한 동기 라우트 → 계약 operationId 매핑.
# 구현 경로 이름은 계약과 다르다(예: POST /validate vs 계약의 /spec/validate).
# 이 매핑이 그 차이를 명시적으로 고정해, 한쪽만 바뀌는 조용한 드리프트를 막는다 (#209).
_IMPLEMENTED_OPERATIONS = {
    "validateSpec",  # POST /validate
    "previewBuild",  # POST /preview
    "createBuild",  # POST /build (동기; 계약은 비동기 POST /builds 지향)
    "listBuildArtifacts",  # GET /artifacts/{run_id}
}
# 아직 builder service에 구현되지 않은(계약상 존재하는) 오퍼레이션.
_PLANNED_OPERATIONS = {
    "listDatasets",
    "getBuild",
    "getBuildManifest",
    "publishArtifacts",
}


def _contract_operation_ids() -> set[str]:
    paths = _load_contract()["paths"]
    return {
        operation["operationId"]
        for methods in paths.values()
        for operation in methods.values()
        if isinstance(operation, dict) and "operationId" in operation
    }


def test_implemented_and_planned_cover_all_contract_operations() -> None:
    # 구현됨 + 계획됨의 합집합이 계약의 모든 오퍼레이션을 빠짐없이 덮어야 한다.
    # 계약에 새 오퍼레이션이 추가되면 이 테스트가 깨져 분류를 강제한다 (#209).
    contract_ops = _contract_operation_ids()

    assert contract_ops >= _IMPLEMENTED_OPERATIONS
    assert contract_ops >= _PLANNED_OPERATIONS
    assert contract_ops == _IMPLEMENTED_OPERATIONS | _PLANNED_OPERATIONS


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
