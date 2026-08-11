"""빌더 명세 검증 루틴 (Medallion 재구성: 기존 validator.py에서 이동).

이 모듈은 BuildSpec이 최소 실행 요건을 충족하는지 검사한다. exporter
레지스트리에 의존하므로 spec 패키지 __init__에서 re-export 하지 않고
``kpubdata_builder.spec.validator`` 경로로 직접 가져온다 (순환 import 회피).

주요 함수:
    - validate_spec: dataset_id, sources, exports 같은 필수 조건 검증

BL3 (#417): problems를 {code, path, message, hint} 객체 배열로 구조화.
기존 문자열 problems 소비자를 위해 message는 항상 포함.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..errors import ValidationError
from ..exporters import EXPORTER_REGISTRY
from ..tabular.polars_helpers import _NAMED_DTYPES
from .models import BuildSpec


@dataclass(frozen=True)
class ValidationProblem:
    """구조화된 검증 문제 (#417).

    code: 기계가 읽을 수 있는 원인 코드.
    path: 필드 경로 (예: sources[0].params.base_date).
    message: 사람이 읽을 수 있는 설명 (항상 포함 — 기존 호환).
    hint: 수정 제안 (선택).
    """

    code: str
    path: str
    message: str
    hint: str | None = None

    def __str__(self) -> str:
        return self.message


def _p(code: str, path: str, message: str, hint: str | None = None) -> ValidationProblem:
    return ValidationProblem(code=code, path=path, message=message, hint=hint)


def validate_spec(spec: BuildSpec) -> None:
    """BuildSpec의 최소 실행 가능성을 검증한다.

    예외:
        ValidationError: 하나 이상의 검증 규칙을 만족하지 못한 경우.
    """
    problems: list[ValidationProblem] = []
    if not spec.dataset_id.strip():
        problems.append(_p("empty_field", "dataset_id", "dataset_id must be a non-empty string"))
    if not spec.title.strip():
        problems.append(_p("empty_field", "title", "title must be a non-empty string"))
    if not spec.description.strip():
        problems.append(_p("empty_field", "description", "description must be a non-empty string"))
    if not spec.sources:
        problems.append(_p("missing_sources", "sources", "at least one source is required"))
    for i, source in enumerate(spec.sources):
        if not source.provider.strip():
            problems.append(
                _p(
                    "empty_field",
                    f"sources[{i}].provider",
                    f"sources[{i}].provider must be a non-empty string",
                )
            )
        if not source.dataset.strip():
            problems.append(
                _p(
                    "empty_field",
                    f"sources[{i}].dataset",
                    f"sources[{i}].dataset must be a non-empty string",
                )
            )
        if source.alias and not source.alias.strip():
            problems.append(
                _p(
                    "blank_alias",
                    f"sources[{i}].alias",
                    f"sources[{i}].alias must not be blank when provided",
                )
            )
    if not spec.exports:
        problems.append(_p("missing_exports", "exports", "at least one export target is required"))
    for i, export in enumerate(spec.exports):
        if not export.output_path.strip():
            problems.append(
                _p(
                    "empty_field",
                    f"exports[{i}].output_path",
                    f"exports[{i}].output_path must be a non-empty string",
                )
            )
        if export.kind not in EXPORTER_REGISTRY:
            supported = sorted(EXPORTER_REGISTRY)
            msg = f"exports[{i}].kind {export.kind!r} is not supported"
            problems.append(
                _p(
                    "unsupported_export_kind",
                    f"exports[{i}].kind",
                    msg,
                    hint=f"Use one of: {', '.join(supported)}",
                )
            )
    for key in spec.metadata:
        if not key.strip():
            problems.append(
                _p("empty_metadata_key", "metadata", "metadata keys must be non-empty strings")
            )
            break
    problems.extend(_split_problems(spec))
    problems.extend(_schema_problems(spec))
    problems.extend(_pii_problems(spec))
    problems.extend(_license_problems(spec))
    if problems:
        raise ValidationError([str(p) for p in problems], structured=problems)


def _split_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """splits 정의의 검증 문제를 모은다."""
    split = spec.splits
    if split is None:
        return []
    problems: list[ValidationProblem] = []
    if split.mode == "ratio":
        if not split.ratios:
            problems.append(
                _p(
                    "missing_ratios",
                    "splits.ratios",
                    "splits.ratios must define at least one split",
                )
            )
        if any(not name.strip() for name in split.ratios):
            problems.append(
                _p(
                    "empty_ratio_name",
                    "splits.ratios",
                    "splits.ratios names must be non-empty strings",
                )
            )
        non_finite = [name for name, f in split.ratios.items() if not math.isfinite(f)]
        if non_finite:
            problems.append(
                _p("non_finite_ratio", "splits.ratios", f"non-finite values: {sorted(non_finite)}")
            )
        elif any(fraction <= 0 for fraction in split.ratios.values()):
            problems.append(
                _p("non_positive_ratio", "splits.ratios", "splits.ratios values must be positive")
            )
        else:
            total = sum(split.ratios.values())
            if split.ratios and abs(total - 1.0) > 1e-6:
                problems.append(
                    _p(
                        "ratio_sum_mismatch",
                        "splits.ratios",
                        f"splits.ratios must sum to 1.0 (got {total})",
                    )
                )
    elif split.mode == "key":
        if not split.key.strip():
            problems.append(
                _p(
                    "empty_split_key",
                    "splits.key",
                    "splits.key must be a non-empty column name for key mode",
                )
            )
    else:
        problems.append(
            _p(
                "unsupported_split_mode",
                "splits.mode",
                f"splits.mode {split.mode!r} is not supported; use 'ratio' or 'key'",
                hint="Use 'ratio' or 'key'",
            )
        )
    return problems


def _schema_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """sources[].schema 계약 자체의 유효성을 검증한다 (#437).

    dtypes/casts 의 문자열이 ``_NAMED_DTYPES`` 키로 해석 가능한지 확인한다.
    로더(loader._parse_schema)는 구조만 검사하고, 여기서 의미를 검사한다 —
    알 수 없는 dtype 문자열이 런타임(정규화/검증)에서야 실패하는 것을 막는다.
    """
    problems: list[ValidationProblem] = []
    supported = sorted(_NAMED_DTYPES)
    for i, source in enumerate(spec.sources):
        if source.schema is None:
            continue
        for col, dtype in source.schema.dtypes.items():
            if dtype.lower() not in _NAMED_DTYPES:
                problems.append(
                    _p(
                        "unknown_dtype",
                        f"sources[{i}].schema.dtypes.{col}",
                        f"unknown dtype {dtype!r} for column {col!r}",
                        hint=f"Use one of: {', '.join(supported)}",
                    )
                )
        for col, cast in source.schema.casts.items():
            if cast.lower() not in _NAMED_DTYPES:
                problems.append(
                    _p(
                        "unknown_cast_dtype",
                        f"sources[{i}].schema.casts.{col}",
                        f"unknown cast dtype {cast!r} for column {col!r}",
                        hint=f"Use one of: {', '.join(supported)}",
                    )
                )
    return problems


def _pii_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """PII 정책의 명백한 위반을 사전에 검증한다 (#441).

    데이터 스캔 자체는 orchestrator 가 silver 이후에 수행한다. 여기서는 정책
    선언 단계의 위반(publish=true + allow 등)만 잡는다.
    """
    problems: list[ValidationProblem] = []
    if spec.pii is None:
        return problems
    # 공개 배포(publish=true) 스펙에서 allow 는 PII 미검출 통과 위험이 있어 금지.
    if spec.publish and spec.pii.mode == "allow":
        problems.append(
            _p(
                "pii_allow_with_publish",
                "pii.mode",
                "pii.mode='allow' is forbidden when publish=true (공개 배포 PII 노출 위험, #441)",
            )
        )
    return problems


def _license_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """publish=true 인 빌드의 라이선스 누락을 사전에 검증한다 (#443).

    kpubdata 가 라이선스 메타데이터를 제공하지 않으므로, 공개 배포 시 사용자
    명시 선언(``license`` 필드)이 재배포 가능성의 유일한 출처이다.
    """
    problems: list[ValidationProblem] = []
    if spec.publish and not spec.license:
        problems.append(
            _p(
                "missing_license_for_publish",
                "license",
                "license is required when publish=true (재배포 가능성 명시, #443)",
            )
        )
    return problems


__all__ = ["ValidationProblem", "validate_spec"]
