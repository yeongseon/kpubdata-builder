"""Automated dataset onboarding pipeline.

Orchestrates: discover → generate spec → verify → record fixtures → PR.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class PipelineResult:
    """Result of the automated pipeline run."""

    dataset_id: str
    step_reached: str  # discover, generate, verify, record, pr
    success: bool
    detail: str = ""
    branch: str = ""
    pr_url: str = ""


def _run(
    cmd: list[str], *, cwd: Path | None = None, timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and return the result."""
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def run_pipeline(
    dataset_id: str,
    *,
    kpubdata_root: Path,
    skip_pr: bool = False,
) -> PipelineResult:
    """Run the full onboarding pipeline for a dataset.

    Steps:
    1. Verify the spec exists and is loadable
    2. Run `make verify DATASET=<id>` to check fixture + replay
    3. If fixtures missing, attempt `make record DATASET=<id>`
    4. Re-verify after recording
    5. Create branch and PR (unless skip_pr=True)

    Parameters:
        dataset_id: Full dataset ID (e.g. datago.apt_trade)
        kpubdata_root: Path to the kpubdata repository root
        skip_pr: If True, stop before creating a PR
    """
    # Step 1: Check spec exists
    result = _run(
        [
            sys.executable, "-c",
            f"from kpubdata.core.spec import find_spec; "
            f"s = find_spec('{dataset_id}'); assert s is not None",
        ],
    )
    if result.returncode != 0:
        return PipelineResult(
            dataset_id=dataset_id,
            step_reached="verify_spec",
            success=False,
            detail=f"Spec not found: {dataset_id}",
        )

    # Step 2: Try verify (may fail if no fixtures)
    verify = _run(["make", "verify", f"DATASET={dataset_id}"], cwd=kpubdata_root, timeout=180)

    if verify.returncode == 0:
        return PipelineResult(
            dataset_id=dataset_id,
            step_reached="verify",
            success=True,
            detail="Already verified — no action needed",
        )

    # Step 3: Try to record fixtures (requires live API key)
    record = _run(
        ["make", "record", f"DATASET={dataset_id}"],
        cwd=kpubdata_root,
        timeout=180,
    )
    if record.returncode != 0:
        stderr_tail = record.stderr.strip().splitlines()[-3:] if record.stderr else []
        return PipelineResult(
            dataset_id=dataset_id,
            step_reached="record",
            success=False,
            detail="Fixture recording failed: " + " | ".join(stderr_tail),
        )

    # Step 4: Re-verify after recording
    re_verify = _run(["make", "verify", f"DATASET={dataset_id}"], cwd=kpubdata_root, timeout=180)
    if re_verify.returncode != 0:
        return PipelineResult(
            dataset_id=dataset_id,
            step_reached="re_verify",
            success=False,
            detail="Verification failed after recording fixtures",
        )

    if skip_pr:
        return PipelineResult(
            dataset_id=dataset_id,
            step_reached="verify",
            success=True,
            detail="Verified — PR creation skipped",
        )

    # Step 5: Create branch + PR
    provider, key = dataset_id.split(".", 1)
    branch = f"agent/{dataset_id}"

    _run(["git", "checkout", "-b", branch], cwd=kpubdata_root)
    _run(["git", "add", "-A"], cwd=kpubdata_root)
    _run(
        ["git", "commit", "-m", f"feat({provider}): add {key} spec + fixtures (agent pipeline)"],
        cwd=kpubdata_root,
    )
    push = _run(["git", "push", "-u", "origin", branch], cwd=kpubdata_root)
    if push.returncode != 0:
        return PipelineResult(
            dataset_id=dataset_id,
            step_reached="push",
            success=False,
            detail=f"Push failed: {push.stderr[:200]}",
            branch=branch,
        )

    pr = _run(
        [
            "gh", "pr", "create",
            "--title", f"feat({provider}): add {key} spec + fixtures (agent pipeline)",
            "--body",
            f"Automated by `kpubdata-builder agent pipeline`.\n\n"
            f"Dataset: `{dataset_id}`\nRef #448",
        ],
        cwd=kpubdata_root,
    )

    pr_url = pr.stdout.strip() if pr.returncode == 0 else ""
    return PipelineResult(
        dataset_id=dataset_id,
        step_reached="pr",
        success=pr.returncode == 0,
        detail=pr_url or f"PR creation failed: {pr.stderr[:200]}",
        branch=branch,
        pr_url=pr_url,
    )
