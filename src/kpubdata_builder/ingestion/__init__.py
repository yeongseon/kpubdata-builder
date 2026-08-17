"""File/URL source ingestion (#498).

Public API source는 기존 kpubdata client 경로(``stages.bronze.build``)를 그대로
쓴다. 이 패키지는 ``kind="file"``/``kind="url"`` source에만 필요한 두 가지를
제공한다.

    - ``url_fetch``: SSRF를 방어하는 안전한 GET(Auth=None) fetch.
    - ``tabular_ingest``: CSV/JSON/JSONL/Parquet 원시 bytes를 레코드로 파싱.

두 경로 모두 최종적으로 기존 Bronze→Silver→Gold pipeline이 소비하는
``dict[str, JsonValue]`` 레코드를 만든다 — 새 pipeline이 아니라 기존 pipeline
앞단에 붙는 resolver다.
"""

from __future__ import annotations

from .errors import IngestionError
from .tabular_ingest import parse_tabular_bytes
from .url_fetch import FetchResult, safe_fetch_get

__all__ = [
    "FetchResult",
    "IngestionError",
    "parse_tabular_bytes",
    "safe_fetch_get",
]
