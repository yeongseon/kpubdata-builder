"""Pipeline modules for kpubdata HuggingFace publishing.

DEPRECATED — legacy publish path (#208).

This package is the script-based publish pipeline used by
``scripts/publish_to_hf.py``. The *canonical* dataset build execution path is
the medallion orchestrator ``kpubdata_builder.pipeline.run_build`` (Bronze →
Silver → Gold), which owns state handling, validation gating, and the single
manifest generator. Add new features and bug fixes to the medallion path; this
package is kept only for backward compatibility with the GitHub Actions
publish workflows and uses its own config schema. See ``docs/ARCHITECTURE.md``.
"""

from pipeline.fetch import fetch_records
from pipeline.package import generate_dataset_card, write_parquet
from pipeline.publish import upload_to_hf
from pipeline.transform import transform_records, validate_schema

__all__ = [
    "fetch_records",
    "transform_records",
    "validate_schema",
    "write_parquet",
    "generate_dataset_card",
    "upload_to_hf",
]
