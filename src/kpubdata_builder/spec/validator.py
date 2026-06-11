"""빌더 명세 검증 루틴 (Medallion 재구성: 기존 validator.py에서 이동).

이 모듈은 BuildSpec이 최소 실행 요건을 충족하는지 검사한다. exporter
레지스트리에 의존하므로 spec 패키지 __init__에서 re-export 하지 않고
``kpubdata_builder.spec.validator`` 경로로 직접 가져온다 (순환 import 회피).

주요 함수:
    - validate_spec: dataset_id, sources, exports 같은 필수 조건 검증
"""

from __future__ import annotations

import math

from ..errors import ValidationError
from ..exporters import EXPORTER_REGISTRY
from .models import BuildSpec


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
    for i, source in enumerate(spec.sources):
        # 빈 provider/dataset이나 공백 alias는 검증을 통과한 뒤 fetch/경로 처리에서
        # 뒤늦게 실패하므로 여기서 막는다 (#191).
        if not source.provider.strip():
            problems.append(f"sources[{i}].provider must be a non-empty string")
        if not source.dataset.strip():
            problems.append(f"sources[{i}].dataset must be a non-empty string")
        if source.alias and not source.alias.strip():
            problems.append(f"sources[{i}].alias must not be blank when provided")
    if not spec.exports:
        problems.append("at least one export target is required")
    for i, export in enumerate(spec.exports):
        if not export.output_path.strip():
            problems.append(f"exports[{i}].output_path must be a non-empty string")
        if export.kind not in EXPORTER_REGISTRY:
            supported = sorted(EXPORTER_REGISTRY)
            problems.append(
                f"exports[{i}].kind {export.kind!r} is not supported; supported kinds: {supported}"
            )
    for key in spec.metadata:
        if not key.strip():
            problems.append("metadata keys must be non-empty strings")
            break
    problems.extend(_split_problems(spec))
    if problems:
        raise ValidationError(problems)


def _split_problems(spec: BuildSpec) -> list[str]:
    """splits 정의의 검증 문제를 모은다."""
    split = spec.splits
    if split is None:
        return []
    problems: list[str] = []
    if split.mode == "ratio":
        if not split.ratios:
            problems.append("splits.ratios must define at least one split")
        if any(not name.strip() for name in split.ratios):
            problems.append("splits.ratios names must be non-empty strings")
        # NaN/inf는 양수·합계 검사를 (NaN 비교가 항상 False라) 조용히 통과하므로,
        # 다른 비율 검사보다 먼저 유한성을 확인한다 (#192).
        non_finite = [
            name for name, fraction in split.ratios.items() if not math.isfinite(fraction)
        ]
        if non_finite:
            problems.append(
                f"splits.ratios values must be finite numbers; non-finite: {sorted(non_finite)}"
            )
        elif any(fraction <= 0 for fraction in split.ratios.values()):
            problems.append("splits.ratios values must be positive")
        else:
            total = sum(split.ratios.values())
            if split.ratios and abs(total - 1.0) > 1e-6:
                problems.append(f"splits.ratios must sum to 1.0 (got {total})")
    elif split.mode == "key":
        if not split.key.strip():
            problems.append("splits.key must be a non-empty column name for key mode")
    else:
        problems.append(f"splits.mode {split.mode!r} is not supported; use 'ratio' or 'key'")
    return problems


__all__ = ["validate_spec"]
