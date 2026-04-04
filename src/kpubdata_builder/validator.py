"""Validation routines for builder specifications."""

from __future__ import annotations

from .spec import BuildSpec


def validate_spec(spec: BuildSpec) -> None:
    """Validate a build specification and raise ValueError on invalid input."""
    if not spec.dataset_id.strip():
        raise ValueError("dataset_id must be a non-empty string")
    if not spec.sources:
        raise ValueError("at least one source is required")
    if not spec.exports:
        raise ValueError("at least one export target is required")
