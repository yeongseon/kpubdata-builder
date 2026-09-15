"""Live dataset verification runner."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .models import CheckName, CheckResult, DatasetStatus, VerifyResult
from .schema_hash import schema_hash

if TYPE_CHECKING:
    from kpubdata.core.spec import SpecDefinition


def _check_endpoint(spec: SpecDefinition) -> CheckResult:
    """Check that the endpoint URL is well-formed and reachable."""
    import urllib.request

    url = spec.endpoint.base_url
    try:
        req = urllib.request.Request(url, method="HEAD")
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=10):  # noqa: S310
            latency = (time.monotonic() - t0) * 1000
        return CheckResult(CheckName.ENDPOINT, passed=True, latency_ms=latency)
    except Exception as exc:
        # Many public APIs reject HEAD but accept GET — a connection
        # error is what we actually want to detect here.
        err_str = str(exc)
        if "403" in err_str or "405" in err_str or "404" in err_str:
            return CheckResult(
                CheckName.ENDPOINT,
                passed=True,
                detail="endpoint reachable (non-GET method rejected)",
            )
        return CheckResult(
            CheckName.ENDPOINT,
            passed=False,
            detail=f"unreachable: {err_str[:120]}",
        )


def _classify_auth_error(exc: Exception) -> DatasetStatus:
    """Map an authentication exception to a dataset status."""
    from kpubdata.exceptions import AuthError, RateLimitError

    if isinstance(exc, RateLimitError):
        return DatasetStatus.RATE_LIMITED
    if isinstance(exc, AuthError):
        msg = str(exc).lower()
        if "활용신청" in msg or "not activated" in msg or "application" in msg:
            return DatasetStatus.NEEDS_APPLICATION
        if "승인" in msg or "approval" in msg or "waiting" in msg:
            return DatasetStatus.WAITING_APPROVAL
        return DatasetStatus.INVALID_KEY
    return DatasetStatus.BROKEN_ENDPOINT


def verify_dataset(
    spec: SpecDefinition,
    *,
    api_key: str | None = None,
    previous_hash: str | None = None,
    page_size: int = 10,
) -> VerifyResult:
    """Run all verification checks against a live API.

    Parameters:
        spec: The dataset spec to verify.
        api_key: API key override. If None, reads from environment.
        previous_hash: Previous schema hash for drift detection.
        page_size: Number of records to request for testing.

    Returns:
        VerifyResult with all check outcomes and final status.
    """
    from kpubdata.config import KPubDataConfig
    from kpubdata.core.executor import (
        SpecExecutor,
        check_payload_error,
        extract_items,
        extract_total_count,
    )
    from kpubdata.core.models import Query
    from kpubdata.core.spec import ExampleSpec
    from kpubdata.exceptions import (
        AuthError,
        PublicDataError,
        RateLimitError,
        TransportError,
    )
    from kpubdata.transport.http import HttpTransport

    result = VerifyResult(dataset_id=spec.id, status=DatasetStatus.HEALTHY)
    total_t0 = time.monotonic()

    # 1. Endpoint check
    endpoint_check = _check_endpoint(spec)
    result.checks.append(endpoint_check)
    if not endpoint_check.passed:
        result.status = DatasetStatus.BROKEN_ENDPOINT
        result.error = endpoint_check.detail
        result.total_latency_ms = (time.monotonic() - total_t0) * 1000
        _fill_skipped(result, after=CheckName.ENDPOINT)
        return result

    # 2-4. Auth + Response + Parser — done via a live API call
    config = KPubDataConfig.from_env()
    if api_key:
        config = KPubDataConfig(
            provider_keys={spec.auth.provider_key or spec.provider: api_key},
            timeout=config.timeout,
            max_retries=1,
        )

    # Pick example params or use defaults
    example = spec.examples[0] if spec.examples else ExampleSpec(name="default")
    query = Query(
        filters=dict(example.params) if hasattr(example, "params") and example.params else {},
        page=example.page or 1,
        page_size=page_size,
    )

    executor = SpecExecutor(HttpTransport(), config)

    try:
        t0 = time.monotonic()
        _params, payload = executor.fetch(spec, query, format_hint=getattr(example, "format", None))
        fetch_latency = (time.monotonic() - t0) * 1000
    except (AuthError, RateLimitError) as exc:
        result.checks.append(
            CheckResult(
                CheckName.AUTH,
                passed=False,
                detail=str(exc)[:120],
            )
        )
        result.status = _classify_auth_error(exc)
        result.error = str(exc)[:200]
        result.total_latency_ms = (time.monotonic() - total_t0) * 1000
        _fill_skipped(result, after=CheckName.AUTH)
        return result
    except TransportError as exc:
        result.checks.append(
            CheckResult(
                CheckName.AUTH,
                passed=True,
                detail="transport reached",
            )
        )
        result.checks.append(
            CheckResult(
                CheckName.RESPONSE,
                passed=False,
                detail=str(exc)[:120],
            )
        )
        result.status = DatasetStatus.BROKEN_ENDPOINT
        result.error = str(exc)[:200]
        result.total_latency_ms = (time.monotonic() - total_t0) * 1000
        _fill_skipped(result, after=CheckName.RESPONSE)
        return result
    except PublicDataError as exc:
        result.checks.append(
            CheckResult(
                CheckName.AUTH,
                passed=True,
            )
        )
        result.checks.append(
            CheckResult(
                CheckName.RESPONSE,
                passed=False,
                detail=str(exc)[:120],
            )
        )
        result.status = DatasetStatus.BROKEN_ENDPOINT
        result.error = str(exc)[:200]
        result.total_latency_ms = (time.monotonic() - total_t0) * 1000
        _fill_skipped(result, after=CheckName.RESPONSE)
        return result

    # Auth passed (we got a response)
    result.checks.append(
        CheckResult(
            CheckName.AUTH,
            passed=True,
            latency_ms=fetch_latency,
        )
    )

    # 3. Response — check envelope error codes
    try:
        check_payload_error(spec, payload)
    except PublicDataError as exc:
        result.checks.append(
            CheckResult(
                CheckName.RESPONSE,
                passed=False,
                detail=str(exc)[:120],
            )
        )
        auth_status = _classify_auth_error(exc)
        result.status = auth_status
        result.error = str(exc)[:200]
        result.total_latency_ms = (time.monotonic() - total_t0) * 1000
        _fill_skipped(result, after=CheckName.RESPONSE)
        return result

    result.checks.append(CheckResult(CheckName.RESPONSE, passed=True))

    # 4. Parser — extract items
    try:
        items = extract_items(spec, payload)
        total_count = extract_total_count(spec, payload)
    except Exception as exc:  # noqa: BLE001
        result.checks.append(
            CheckResult(
                CheckName.PARSER,
                passed=False,
                detail=str(exc)[:120],
            )
        )
        result.status = DatasetStatus.BROKEN_ENDPOINT
        result.error = str(exc)[:200]
        result.total_latency_ms = (time.monotonic() - total_t0) * 1000
        _fill_skipped(result, after=CheckName.PARSER)
        return result

    result.checks.append(
        CheckResult(
            CheckName.PARSER,
            passed=True,
            detail=f"{len(items)} items, total={total_count}",
        )
    )
    result.records_tested = len(items)

    # 5. Pagination — verify total_count is plausible
    pagination_ok = True
    pagination_detail = ""
    if spec.pagination.type == "none":
        pagination_detail = "no pagination (spec: none)"
    elif total_count is not None:
        pagination_detail = f"total_count={total_count}"
        if total_count < 0:
            pagination_ok = False
            pagination_detail = f"invalid total_count={total_count}"
    elif items:
        pagination_detail = f"no total_count, got {len(items)} items"
    else:
        pagination_detail = "empty response, pagination untestable"

    result.checks.append(
        CheckResult(
            CheckName.PAGINATION,
            passed=pagination_ok,
            detail=pagination_detail,
        )
    )

    # 6. Schema — hash field structure and compare
    if items:
        current_hash = schema_hash(items)
        result.schema_hash = current_hash
        result.previous_schema_hash = previous_hash

        if previous_hash and previous_hash != current_hash:
            result.checks.append(
                CheckResult(
                    CheckName.SCHEMA,
                    passed=False,
                    detail=f"changed: {previous_hash[:8]}.. -> {current_hash[:8]}..",
                )
            )
            result.status = DatasetStatus.SCHEMA_CHANGED
        else:
            schema_detail = "unchanged" if previous_hash else f"baseline {current_hash[:8]}.."
            result.checks.append(
                CheckResult(
                    CheckName.SCHEMA,
                    passed=True,
                    detail=schema_detail,
                )
            )
    else:
        result.checks.append(
            CheckResult(
                CheckName.SCHEMA,
                passed=True,
                detail="no items to hash",
            )
        )

    result.total_latency_ms = (time.monotonic() - total_t0) * 1000
    return result


def _fill_skipped(result: VerifyResult, *, after: CheckName) -> None:
    """Fill remaining checks as skipped after a failure."""
    all_checks = list(CheckName)
    seen = {c.name for c in result.checks}
    started = False
    for name in all_checks:
        if name == after:
            started = True
            continue
        if started and name not in seen:
            result.checks.append(CheckResult(name, passed=False, detail="skipped"))


def verify_datasets(
    specs: list[SpecDefinition],
    *,
    previous_hashes: dict[str, str] | None = None,
    page_size: int = 10,
) -> list[VerifyResult]:
    """Verify multiple datasets sequentially.

    Parameters:
        specs: List of spec definitions to verify.
        previous_hashes: Map of dataset_id -> previous schema hash.
        page_size: Records per test request.

    Returns:
        List of VerifyResult for each dataset.
    """
    hashes = previous_hashes or {}
    results: list[VerifyResult] = []
    for spec in specs:
        prev_hash = hashes.get(spec.id)
        result = verify_dataset(spec, previous_hash=prev_hash, page_size=page_size)
        results.append(result)
    return results
