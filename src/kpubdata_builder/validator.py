"""Validation routines for builder specifications."""

from __future__ import annotations

from .errors import ValidationError
from .spec import BuildSpec


def validate_spec(spec: BuildSpec) -> None:
    problems: list[str] = []
    if not spec.dataset_id.strip():
        problems.append("dataset_id must be a non-empty string")
    if not spec.sources:
        problems.append("at least one source is required")
    if not spec.exports:
        problems.append("at least one export target is required")
    if problems:
        raise ValidationError(problems)
