"""순수 파이썬 OpenAPI 3.1 JSON Schema 부분집합 validator (#209, ADR-0005).

외부 의존성(`openapi-core`, `jsonschema`) 없이 `contract/builder-api.yaml`의 응답
스키마 부분집합을 검증한다. ADR-0005의 "무외부의존·stdlib 결정성" 원칙을 지키면서
미해결 질문 #1(스키마 대조를 순수 파이썬 경량으로 할지, 검증 라이브러리를 도입할지)을
"순수 파이썬 경량" 방향으로 마무리한다.

정적 계약 검증(경로/상태코드/operationId 대조 — #317, #319)이 YAML의 *선언*과
`dispatch`의 *라우팅 목록*이 일치하는지 본다면, 이 validator는 한 단계 더 나아가
**실제 dispatch 응답 본문이 선언된 스키마에 부합하는지**(wire-level conformance)를
검증한다. app.py가 응답에서 필수 필드를 빼거나 타입을 바꾸되 YAML을 갱신하지
않으면, 정적 검사는 잡지 못하지만 이 검사가 잡는다 — 이것이 ADR-0005가 막고자 하는
드리프트의 실체다.

지원 키워드(이 계약이 사용하는 부분집합만):
    - ``$ref`` (``#/components/schemas/Name`` 형태의 로컬 참조만)
    - ``type`` (단일 문자열 또는 null 포함 합집합: ``[string, "null"]``)
    - ``required``, ``properties``
    - ``additionalProperties`` (``true`` | ``false`` | 스키마)
    - ``items`` (array)
    - ``enum``
    - ``oneOf`` (적어도 한 분기가 통과하면 유효 — conformance 게이트는 실제 드리프트를
      잡는 것이 목적이므로, 여러 분기에 동시에 맞더라도 거짓 양성을 피하기 위해 관대하게
      해석한다)
    - ``not``, ``allOf``
    - ``minimum`` (정수/숫자 하한), ``minItems`` (배열 최소 길이)

알 수 없는 키워드는 무시한다(전방 호환). 추가 선택 필드는 기본 허용하므로, 응답에
새 선택 필드가 더해지는 *부가적* 변화는 통과시키고, 필수 필드 누락·타입 변경 같은
*구조적 회귀*만 실패시킨다.
"""

from __future__ import annotations

from typing import Any

# OpenAPI/JSON Schema 문서와 JSON 값은 모두 임의 중첩 구조다. 테스트 보조 모듈이므로
# 정확한 재귀 별칭 대신 Any를 쓴다(mypy는 src/만 검사하므로 여기엔 영향 없음).
Schema = dict[str, Any]
Json = Any


def resolve_ref(contract: Schema, ref: str) -> Schema:
    """``#/components/schemas/Name`` 형태의 로컬 $ref를 실제 스키마로 해석한다.

    외부/원격 참조는 이 계약에서 쓰지 않으므로 거부한다.
    """
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported $ref (only local '#/' allowed): {ref}")
    node: Any = contract
    for part in ref.lstrip("#/").split("/"):
        if not isinstance(node, dict) or part not in node:
            raise ValueError(f"unresolved $ref: {ref}")
        node = node[part]
    if not isinstance(node, dict):
        raise ValueError(f"$ref target is not a mapping: {ref}")
    return node


def _matches_type(value: Json, type_name: str) -> bool:
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        # bool은 int의 하위 타입이지만 JSON integer 의미가 아니므로 제외한다.
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "null":
        return value is None
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _type_names(schema: Schema) -> list[str]:
    raw = schema.get("type")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


def validate(value: Json, schema: Schema, contract: Schema, path: str = "$") -> list[str]:
    """``value``가 ``schema``를 만족하는지 검증하고 위반 목록을 반환한다.

    반환값이 빈 리스트면 유효하다. 각 위반은 사람이 읽을 수 있는 문자열이며,
    ``path``(기본 ``$``)는 JSON 문서 내 위치를 가리킨다.
    """
    # $ref가 있으면 다른 키워드와 함께 쓰이지 않으므로(OpenAPI 규칙) 해석 후 위임한다.
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return validate(value, resolve_ref(contract, ref), contract, path)

    errors: list[str] = []

    # oneOf: 적어도 한 분기가 통과하면 유효(관대한 해석 — 의도는 위 모듈 docstring 참고).
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        passing = [
            branch
            for branch in one_of
            if isinstance(branch, dict) and not validate(value, branch, contract, path)
        ]
        if not passing:
            errors.append(f"{path}: value matched no oneOf branch")
        return errors

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for branch in all_of:
            if isinstance(branch, dict):
                errors.extend(validate(value, branch, contract, path))

    not_schema = schema.get("not")
    if isinstance(not_schema, dict) and not validate(value, not_schema, contract, path):
        errors.append(f"{path}: value matched forbidden 'not' schema")

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum {schema['enum']!r}")

    type_names = _type_names(schema)
    if type_names and not any(_matches_type(value, name) for name in type_names):
        errors.append(f"{path}: expected type {type_names}, got {type(value).__name__}")
        # 타입 자체가 다르면 하위 구조를 더 검사해 봐야 의미가 없다.
        return errors

    object_keywords = {"required", "properties", "additionalProperties"}
    if isinstance(value, dict) and (
        "object" in type_names or any(keyword in schema for keyword in object_keywords)
    ):
        errors.extend(_validate_object(value, schema, contract, path))
    elif "array" in type_names and isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: array length {len(value)} < minItems {schema['minItems']}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(validate(item, item_schema, contract, f"{path}[{index}]"))

    if (
        "minimum" in schema
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value < schema["minimum"]
    ):
        errors.append(f"{path}: {value} < minimum {schema['minimum']}")

    return errors


def _validate_object(
    value: dict[str, Json], schema: Schema, contract: Schema, path: str
) -> list[str]:
    errors: list[str] = []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        properties = {}

    for key in schema.get("required", []):
        if key not in value:
            errors.append(f"{path}: missing required property {key!r}")

    for key, sub in value.items():
        child_path = f"{path}.{key}"
        prop_schema = properties.get(key)
        if isinstance(prop_schema, dict):
            errors.extend(validate(sub, prop_schema, contract, child_path))
            continue
        # 선언되지 않은 프로퍼티 → additionalProperties 정책으로 판정.
        addl = schema.get("additionalProperties", True)
        if addl is False:
            errors.append(f"{child_path}: extra property not allowed")
        elif isinstance(addl, dict):
            errors.extend(validate(sub, addl, contract, child_path))
        # True / 생략 → 추가 프로퍼티 허용(부가적 변화 허용).
    return errors


def _normalize_path(contract: Schema, path: str) -> str:
    """구체적 경로(예: ``/artifacts/run-1``)를 템플릿(``/artifacts/{run_id}``)에 맞춘다."""
    paths = contract.get("paths")
    if not isinstance(paths, dict):
        return path
    if path in paths:
        return path
    path_parts = path.split("/")
    for template in paths:
        tmpl_parts = template.split("/")
        if len(tmpl_parts) != len(path_parts):
            continue
        # 길이가 같음은 위에서 continue로 보장했으므로 strict=True가 안전하다.
        if all(
            tp.startswith("{") or tp == pp for tp, pp in zip(tmpl_parts, path_parts, strict=True)
        ):
            return template
    return path


def response_schema(contract: Schema, path: str, method: str, status_code: int) -> Schema | None:
    """``(path, method, status_code)``에 대응하는 응답 JSON 스키마를 반환한다.

    경로 템플릿 정규화와 ``content/application/json`` 추출을 담당한다.
    해당 상태 코드가 계약에 선언되어 있지 않으면 ``None``을 반환한다.
    """
    paths = contract.get("paths")
    if not isinstance(paths, dict):
        return None
    norm = _normalize_path(contract, path)
    operation = paths.get(norm)
    if not isinstance(operation, dict):
        return None
    responses = operation.get(method.lower())
    if not isinstance(responses, dict):
        return None
    response = responses.get("responses", {}).get(str(status_code))
    if not isinstance(response, dict):
        return None
    content = response.get("content", {})
    app_json = content.get("application/json")
    schema = app_json.get("schema") if isinstance(app_json, dict) else None
    return schema if isinstance(schema, dict) else None
