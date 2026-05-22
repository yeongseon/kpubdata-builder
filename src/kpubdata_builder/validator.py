"""Validation routines for builder specifications."""

from __future__ import annotations

from .errors import ValidationError
from .exporters import EXPORTER_REGISTRY
from .spec import BuildSpec


def validate_spec(spec: BuildSpec) -> None:
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
