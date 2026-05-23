"""빌더 명세를 위한 검증 루틴.

이 모듈은 BuildSpec이 최소 실행 요건을 충족하는지 빠르게 검사한다.

주요 함수:
    - validate_spec: dataset_id, sources, exports 같은 필수 조건 검증
"""

from __future__ import annotations

from .errors import ValidationError
from .exporters import EXPORTER_REGISTRY
from .spec import BuildSpec


def validate_spec(spec: BuildSpec) -> None:
    """BuildSpec의 최소 실행 가능성을 검증한다.

    매개변수:
        spec: 검증할 빌드 명세.

    반환값:
        없음.

    예외:
        ValidationError: 하나 이상의 검증 규칙을 만족하지 못한 경우.
    """
    problems: list[str] = []
    if not spec.dataset_id.strip():
        problems.append("dataset_id must be a non-empty string")
    if not spec.title.strip():
        problems.append("title must be a non-empty string")
    if not spec.description.strip():
        problems.append("description must be a non-empty string")
    if not spec.sources:
        problems.append("at least one source is required")
    if not spec.exports:
        problems.append("at least one export target is required")
    for i, export in enumerate(spec.exports):
        if not export.output_path.strip():
            problems.append(f"exports[{i}].output_path must be a non-empty string")
        if export.kind not in EXPORTER_REGISTRY:
            supported = sorted(EXPORTER_REGISTRY)
            problems.append(
                f"exports[{i}].kind {export.kind!r} is not supported; "
                f"supported kinds: {supported}"
            )
    for key in spec.metadata:
        if not key.strip():
            problems.append("metadata keys must be non-empty strings")
            break
    if problems:
        raise ValidationError(problems)
