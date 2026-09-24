"""Dataset live verification engine.

Verifies kpubdata spec datasets against their live APIs, checking endpoint
accessibility, authentication, response parsing, pagination, and schema
stability.
"""

from __future__ import annotations

from .models import CheckName, CheckResult, DatasetStatus, VerifyResult
from .runner import verify_dataset, verify_datasets
from .schema_hash import schema_hash

__all__ = [
    "CheckName",
    "CheckResult",
    "DatasetStatus",
    "VerifyResult",
    "schema_hash",
    "verify_dataset",
    "verify_datasets",
]
