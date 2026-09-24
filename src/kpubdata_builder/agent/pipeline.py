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
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 120,
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


def _agent_output_paths(*, provider: str, key: str) -> tuple[str, ...]:
    """이 파이프라인이 만드는 파일들. 이것만 커밋한다."""
    return (
        f"src/kpubdata/providers/{provider}/specs/{key}.yaml",
        f"tests/fixtures/{provider}/{key}",
    )


def _is_agent_output(path: str, *, provider: str, key: str) -> bool:
    prefixes = _agent_output_paths(provider=provider, key=key)
    return any(path.startswith(prefix) for prefix in prefixes)


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
    # dataset_id 를 소스 문자열에 끼워 넣지 않는다. 따옴표 하나만 들어와도
    # 임의 코드가 되고, 이 함수는 CLI 인자와 HTTP 경로에서 값을 받는다.
    # argv 로 넘기면 문자열은 어디까지나 데이터다.
    result = _run(
        [
            sys.executable,
            "-c",
            "import sys; from kpubdata.core.spec import find_spec; "
            "sys.exit(0 if find_spec(sys.argv[1]) is not None else 1)",
            dataset_id,
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

    # 더럽혀진 작업 트리 위에서 커밋하지 않는다. `git add -A` 로 남의 변경과
    # 도구가 남긴 파일까지 함께 올린 적이 있다(kpubdata 저장소의 .omx/ 가 그렇게
    # 들어갔다). 이 파이프라인이 만든 것만 올린다.
    dirty = _run(["git", "status", "--porcelain"], cwd=kpubdata_root)
    unrelated = [
        line
        for line in dirty.stdout.splitlines()
        if line[3:] and not _is_agent_output(line[3:], provider=provider, key=key)
    ]
    if unrelated:
        return PipelineResult(
            dataset_id=dataset_id,
            step_reached="commit",
            success=False,
            detail=(
                "kpubdata working tree has unrelated changes; "
                f"commit or stash them first ({len(unrelated)} path(s))"
            ),
        )

    _run(["git", "checkout", "-b", branch], cwd=kpubdata_root)
    for path in _agent_output_paths(provider=provider, key=key):
        _run(["git", "add", "--", path], cwd=kpubdata_root)
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
            "gh",
            "pr",
            "create",
            "--title",
            f"feat({provider}): add {key} spec + fixtures (agent pipeline)",
            "--body",
            f"Automated by `kpubdata-builder agent pipeline`.\n\nDataset: `{dataset_id}`\nRef #448",
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
