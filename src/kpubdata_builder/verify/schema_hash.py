"""Schema fingerprinting for drift detection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence


def _extract_structure(items: Sequence[dict[str, object]]) -> dict[str, str]:
    """Extract a canonical field→type mapping from response items.

    Merges keys across all items and infers each key's type from the first
    non-None value seen for it. ``null`` is provisional: APIs routinely return
    null for optional fields, so fixing a field's type on the first null would
    make the baseline depend on row order and hide a real type change later in
    the page.
    """
    field_types: dict[str, str] = {}
    for item in items:
        for key, value in item.items():
            if field_types.get(key, "null") != "null":
                continue
            if value is None:
                field_types.setdefault(key, "null")
            elif isinstance(value, bool):
                field_types[key] = "boolean"
            elif isinstance(value, int):
                field_types[key] = "integer"
            elif isinstance(value, float):
                field_types[key] = "number"
            elif isinstance(value, str):
                field_types[key] = "string"
            elif isinstance(value, list):
                field_types[key] = "array"
            elif isinstance(value, dict):
                field_types[key] = "object"
            else:
                field_types[key] = "unknown"
    return field_types


def schema_hash(items: Sequence[dict[str, object]]) -> str:
    """Compute a stable hash of the response field structure.

    The hash captures field names and their inferred types, but not values.
    This allows detection of added/removed/type-changed fields.
    """
    structure = _extract_structure(items)
    canonical = json.dumps(structure, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def schema_diff(
    old_items: Sequence[dict[str, object]],
    new_items: Sequence[dict[str, object]],
) -> dict[str, list[str]]:
    """Compare field structures and return added/removed/changed fields."""
    old_struct = _extract_structure(old_items)
    new_struct = _extract_structure(new_items)

    old_keys = set(old_struct)
    new_keys = set(new_struct)

    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    changed = sorted(k for k in old_keys & new_keys if old_struct[k] != new_struct[k])

    return {"added": added, "removed": removed, "type_changed": changed}
