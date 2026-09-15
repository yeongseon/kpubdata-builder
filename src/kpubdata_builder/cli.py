"""kpubdata-builder용 명령줄 진입점.

이 모듈은 argparse 기반 CLI를 구성하고, validate/preview/build/publish/serve 명령의
진입점을 제공한다.

주요 함수:
    - build_parser: 하위 명령을 포함한 ArgumentParser 구성
    - dispatch: 파싱된 명령을 실제 실행 함수로 분기
    - main: CLI 프로세스용 최상위 진입점
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from . import __version__
from .errors import PublishError, SpecLoadError, ValidationError
from .pipeline import preview_build, run_build
from .publishers import PUBLISHER_REGISTRY
from .spec import load_spec
from .spec.validator import validate_spec
from .stages.bronze.build import SourceClient
from .tabular import DEFAULT_PREVIEW_LIMIT


def build_parser() -> argparse.ArgumentParser:
    """CLI 전용 ArgumentParser를 생성한다.

    validate, preview, build 하위 명령을 등록하고 공통 --version 옵션도
    함께 노출한다.

    반환값:
        argparse.ArgumentParser: 구성 완료된 파서 객체.

    예시:
        >>> parser = build_parser()
        >>> parser.prog
        'kpubdata-builder'
    """
    parser = argparse.ArgumentParser(
        prog="kpubdata-builder",
        description="KPubData Builder command-line interface.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="command")

    validate_cmd = subparsers.add_parser(
        "validate",
        help="Validate a BuildSpec YAML file.",
    )
    validate_cmd.add_argument("spec", help="Path to the BuildSpec YAML file.")

    preview_cmd = subparsers.add_parser(
        "preview",
        help="Preview a BuildSpec: schema and sample rows without writing artifacts.",
    )
    preview_cmd.add_argument("spec", help="Path to the BuildSpec YAML file.")
    preview_cmd.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_PREVIEW_LIMIT,
        help=f"Maximum sample rows per source (default: {DEFAULT_PREVIEW_LIMIT}).",
    )

    build_cmd = subparsers.add_parser(
        "build",
        help="Execute a BuildSpec through the Medallion pipeline.",
    )
    build_cmd.add_argument("spec", help="Path to the BuildSpec YAML file.")
    build_cmd.add_argument(
        "--output-dir",
        default="build",
        help="Run workspace root directory (default: build).",
    )
    build_cmd.add_argument(
        "--run-id",
        default=None,
        help="Run identifier (default: generated timestamp).",
    )

    publish_cmd = subparsers.add_parser(
        "publish",
        help="Publish build artifacts to a local or remote destination.",
    )
    publish_cmd.add_argument("spec", help="Path to the BuildSpec YAML file.")
    publish_cmd.add_argument(
        "--target",
        choices=sorted(PUBLISHER_REGISTRY.keys()),
        default="local",
        help="Publish target (default: local).",
    )
    publish_cmd.add_argument(
        "--destination",
        required=True,
        help="Local directory path (local) or HF repo id (huggingface).",
    )
    publish_cmd.add_argument(
        "--artifacts-dir",
        required=True,
        help="Directory whose files will be published.",
    )
    publish_cmd.add_argument(
        "--public",
        action="store_true",
        help="Create new datasets as public (kaggle only; default: private).",
    )

    serve_cmd = subparsers.add_parser(
        "serve",
        help="Run the Builder HTTP service.",
    )
    serve_cmd.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host (default: 127.0.0.1).",
    )
    serve_cmd.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Bind port (default: 8000).",
    )
    serve_cmd.add_argument(
        "--output-dir",
        default="build",
        help="Run workspace root directory (default: build).",
    )
    serve_cmd.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Max concurrent request threads (default: 10, or KPUBDATA_BUILDER_MAX_WORKERS).",
    )

    rebuild_cmd = subparsers.add_parser(
        "rebuild-index",
        help="Rebuild the build index from filesystem scans.",
    )
    rebuild_cmd.add_argument(
        "--output-dir",
        default="build",
        help="Run workspace root directory (default: build).",
    )

    # -- Agent pipeline commands --

    discover_cmd = subparsers.add_parser(
        "discover",
        help="Discover API metadata from a data.go.kr URL.",
    )
    discover_cmd.add_argument(
        "url",
        help="data.go.kr API detail page URL.",
    )
    discover_cmd.add_argument(
        "--dataset-id",
        default=None,
        help="Override the generated dataset ID (e.g. datago.ocean_buoy).",
    )
    discover_cmd.add_argument(
        "--output",
        default=None,
        help="Write generated spec YAML to this file path.",
    )

    monitor_cmd = subparsers.add_parser(
        "monitor",
        help="Check pending dataset applications for approval.",
    )
    monitor_cmd.add_argument(
        "--state-file",
        default=".kpubdata-monitor.yaml",
        help="Path to monitor state file (default: .kpubdata-monitor.yaml).",
    )
    monitor_cmd.add_argument(
        "--add",
        default=None,
        help="Add a dataset ID to the pending list.",
    )
    monitor_cmd.add_argument(
        "--check",
        action="store_true",
        help="Check all pending datasets for approval status.",
    )

    pipeline_cmd = subparsers.add_parser(
        "pipeline",
        help="Run automated onboarding pipeline for a dataset.",
    )
    pipeline_cmd.add_argument(
        "dataset",
        help="Dataset ID to process (e.g. datago.ocean_buoy).",
    )
    pipeline_cmd.add_argument(
        "--kpubdata-root",
        default=None,
        help="Path to kpubdata repository root (default: auto-detect).",
    )
    pipeline_cmd.add_argument(
        "--skip-pr",
        action="store_true",
        help="Stop after verification, do not create a PR.",
    )

    prune_cmd = subparsers.add_parser(
        "prune-cancelled",
        help="List (and optionally delete) cancelled partial-run artifacts past a TTL (#549).",
    )
    prune_cmd.add_argument(
        "--output-dir",
        default="build",
        help="Run workspace root directory (default: build).",
    )
    prune_cmd.add_argument(
        "--ttl-hours",
        type=float,
        default=None,
        help=(
            "Retention window in hours for cancelled partial runs. "
            "Defaults to KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS; "
            "when unset, nothing is ever a deletion candidate."
        ),
    )
    prune_cmd.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete matching run workspaces. Without this flag the command is a dry run.",
    )

    return parser


def _create_client(
    *,
    provider_keys: dict[str, str] | None = None,
    timeout: float | None = None,
    cache: bool | None = None,
) -> SourceClient:
    """kpubdata 클라이언트를 환경설정으로 생성한다.

    테스트에서 monkeypatch로 대체할 수 있도록 별도 함수로 분리한다. 실제
    네트워크 호출은 build 실행(run_build) 시점에만 발생한다.
    """
    from kpubdata import Client

    # kpubdata #276 이후 from_env는 명시적 파라미터만 받는다(**kwargs 폐기).
    return cast(
        SourceClient,
        Client.from_env(
            provider_keys=provider_keys,
            timeout=timeout,
            cache=cache,
        ),
    )


def _run_validate(spec_path: str) -> int:
    """지정한 BuildSpec 파일을 로드하고 검증한다.

    매개변수:
        spec_path: 검사할 YAML 파일 경로 문자열.

    반환값:
        int: 성공 시 0, 로드/검증 실패 시 1.

    예외:
        직접 예외를 전파하지 않고 오류 메시지와 종료 코드로 변환한다.
    """
    try:
        spec = load_spec(Path(spec_path))
        validate_spec(spec)
    except SpecLoadError as exc:
        print(f"error: failed to load spec: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print("error: spec validation failed:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"spec is valid: {spec.dataset_id}")
    return 0


def _run_build(spec_path: str, *, output_dir: str, run_id: str | None) -> int:
    """BuildSpec을 로드·검증한 뒤 Medallion 파이프라인을 실행한다.

    매개변수:
        spec_path: 빌드할 BuildSpec YAML 경로.
        output_dir: 실행 워크스페이스 루트.
        run_id: 실행 식별자. None이면 타임스탬프로 생성.

    반환값:
        int: 모든 소스 성공 시 0, 로드/검증/빌드 실패 시 1.
    """
    try:
        spec = load_spec(Path(spec_path))
        validate_spec(spec)
    except SpecLoadError as exc:
        print(f"error: failed to load spec: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print("error: spec validation failed:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    client = _create_client()
    result = run_build(spec, client=client, output_root=Path(output_dir), run_id=run_id)

    print(f"build: {spec.dataset_id} (run {result.context.run_id})")
    for outcome in result.outcomes:
        stages = ", ".join(outcome.stages_completed) or "-"
        print(f"  - {outcome.source_key}: {outcome.status} [{stages}]")
    print(f"manifest: {result.manifest_path}")

    if result.status != "ok":
        print("error: build failed for one or more sources", file=sys.stderr)
        for outcome in result.outcomes:
            if outcome.status == "failed":
                print(f"  - {outcome.source_key}: {outcome.error}", file=sys.stderr)
        return 1
    return 0


def _run_preview(spec_path: str, *, limit: int) -> int:
    """BuildSpec을 로드·검증한 뒤 각 소스의 스키마와 샘플만 출력한다.

    실제 아티팩트 파일은 만들지 않는다.

    매개변수:
        spec_path: 미리볼 BuildSpec YAML 경로.
        limit: 소스별 샘플 최대 행 수.

    반환값:
        int: 성공 시 0, 로드/검증 실패 시 1.
    """
    try:
        spec = load_spec(Path(spec_path))
        validate_spec(spec)
    except SpecLoadError as exc:
        print(f"error: failed to load spec: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print("error: spec validation failed:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    try:
        client = _create_client()
        result = preview_build(spec, client=client, limit=limit)
    except ValueError as exc:
        # limit < 1 같은 사용자 입력 오류.
        print(f"error: invalid preview input: {exc}", file=sys.stderr)
        return 1

    print(f"preview: {spec.dataset_id}")
    failed_sources: list[str] = []
    for source in result.previews:
        if source.status != "ok":
            failed_sources.append(source.source_key)
            continue
        columns = ", ".join(f"{c.name} ({c.dtype})" for c in source.schema.columns)
        print(f"  - {source.source_key}: {columns}")
        print(f"    sample ({len(source.preview.rows)} of {source.preview.total_rows} rows):")
        for row in source.preview.rows:
            print(f"      {row}")

    if failed_sources:
        # 소스 fetch 실패는 stderr + exit 1 — CI/자동화가 성공으로 오판하지 않도록.
        print("error: preview failed for one or more sources", file=sys.stderr)
        for source in result.previews:
            if source.status != "ok":
                print(f"  - {source.source_key}: {source.error}", file=sys.stderr)
        return 1
    return 0


def _run_publish(
    spec_path: str,
    *,
    target: str,
    destination: str,
    artifacts_dir: str,
    public: bool = False,
) -> int:
    """BuildSpec을 로드·검증한 뒤 지정한 target에 산출물을 게시한다.

    매개변수:
        spec_path: 게시 기준 BuildSpec YAML 경로.
        target: 게시 대상 식별자 (PUBLISHER_REGISTRY 키).
        destination: 로컬 디렉터리 경로 또는 원격 repo id.
        artifacts_dir: 게시할 파일이 있는 디렉터리.
        public: kaggle 신규 데이터셋을 공개로 만들지 여부 (다른 target은 무시).

    반환값:
        int: 성공 시 0, 로드/검증/게시 실패 시 1.
    """
    try:
        spec = load_spec(Path(spec_path))
        validate_spec(spec)
    except SpecLoadError as exc:
        print(f"error: failed to load spec: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print("error: spec validation failed:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    artifacts_path = Path(artifacts_dir)
    if not artifacts_path.is_dir():
        print(f"error: no artifacts found in {artifacts_dir}", file=sys.stderr)
        return 1

    publisher = PUBLISHER_REGISTRY[target]

    # 레이아웃 단위(Kaggle)는 디렉터리 자체를, 파일 단위(local/HF)는 개별 파일을
    # 전달한다. 이렇게 publisher별 입력 계약 불일치를 해소한다 (#176).
    paths: tuple[Path, ...]
    if publisher.expects_directory:
        paths = (artifacts_path,)
    else:
        paths = tuple(sorted(p for p in artifacts_path.rglob("*") if p.is_file()))
        if not paths:
            print(f"error: no artifacts found in {artifacts_dir}", file=sys.stderr)
            return 1

    publish_kwargs: dict[str, object] = {"destination": destination}
    if target == "kaggle":
        publish_kwargs["public"] = public

    try:
        result = publisher.publish(paths, **publish_kwargs)  # type: ignore[arg-type]
    except (PublishError, RuntimeError) as exc:
        print(f"error: publish failed: {exc}", file=sys.stderr)
        return 1

    print(f"publish: {spec.dataset_id} -> {target}")
    print(f"  target: {result.reference}")
    print(f"  artifacts: {result.artifact_count}")
    return 0


def _run_serve(*, output_dir: str, host: str, port: int, max_workers: int | None) -> int:
    """BuilderService를 HTTP 서버로 실행한다 (#249).

    매개변수:
        output_dir: 실행 워크스페이스 루트.
        host: 바인딩 호스트.
        port: 바인딩 포트.
        max_workers: 동시 요청 스레드 상한. None이면 KPUBDATA_BUILDER_MAX_WORKERS env,
            그것도 없으면 기본값(10)을 쓴다 (#374).

    반환값:
        int: 종료 코드. Ctrl-C/ SIGTERM 우아한 종료 시 0.
    """
    from .service import BuilderService
    from .service.http import _DEFAULT_MAX_WORKERS, serve

    # 우선순위: --max-workers 플래그 > KPUBDATA_BUILDER_MAX_WORKERS env > 기본값.
    if max_workers is None:
        env_workers = os.environ.get("KPUBDATA_BUILDER_MAX_WORKERS")
        max_workers = int(env_workers) if env_workers else _DEFAULT_MAX_WORKERS
    if max_workers < 1:
        raise SystemExit(f"max_workers must be >= 1, got {max_workers}")

    service = BuilderService(
        output_root=Path(output_dir),
        client_factory=_create_client,
        async_max_workers=max_workers,
    )
    # 장시간 실행 명령이므로 시작 로그가 파이프 버퍼링에 갈리지 않도록 즉시 flush한다.
    print(
        f"serving kpubdata-builder on http://{host}:{port} "
        f"(output: {output_dir}, max_workers: {max_workers})",
        flush=True,
    )
    try:
        serve(service, host=host, port=port, max_workers=max_workers)
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    return 0


def _run_rebuild_index(output_dir: str) -> int:
    """파일시스템 스캔으로 빌드 인덱스를 재구축한다 (#309, ADR 0003).

    매개변수:
        output_dir: 빌드 출력 루트 디렉터리.

    반환값:
        int: 성공 시 0, 실패 시 1.
    """
    from .store import rebuild_index

    output_root = Path(output_dir)
    print(f"rebuilding build index from {output_root}...", flush=True)

    try:
        count = rebuild_index(output_root)
        print(f"rebuilt index with {count} build(s)", flush=True)
        return 0
    except Exception as exc:
        print(f"error: failed to rebuild index: {exc}", file=sys.stderr)
        return 1


def _run_prune_cancelled(*, output_dir: str, ttl_hours: float | None, apply: bool) -> int:
    """TTL이 지난 cancelled+partial run 산출물을 나열/정리한다 (#549).

    기본은 dry-run이고, ``--apply``를 줘야 삭제가 일어난다. TTL은 인자가
    우선이고, 없으면 ``KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS`` 환경변수,
    그마저 없으면 비활성(대상 없음)이다 — 잘못된 설정으로 증거가 사라지는
    일은 없다.
    """
    import os

    from .retention import CANCELLED_RUN_TTL_ENV, prune_cancelled_runs

    output_root = Path(output_dir)
    effective_ttl = ttl_hours
    if effective_ttl is None:
        raw_env = os.environ.get(CANCELLED_RUN_TTL_ENV, "").strip()
        if raw_env:
            try:
                effective_ttl = float(raw_env)
            except ValueError:
                print(
                    f"error: {CANCELLED_RUN_TTL_ENV}={raw_env!r} is not a number of hours",
                    file=sys.stderr,
                )
                return 1

    mode = "APPLY (deleting)" if apply else "dry run (nothing will be deleted)"
    print(f"pruning cancelled partial runs under {output_root} — {mode}", flush=True)

    report = prune_cancelled_runs(output_root, ttl_hours=effective_ttl, apply=apply)

    for candidate in report.kept:
        print(f"kept: {candidate.run_id}", flush=True)
    for run_id in report.deleted:
        print(f"deleted: {run_id}", flush=True)
    print(
        f"scanned {report.scanned} cancelled partial run(s), deleted {report.deleted_count}",
        flush=True,
    )
    return 0


def _run_discover(url: str, *, dataset_id: str | None, output: str | None) -> int:
    """Discover API metadata from a data.go.kr URL."""
    from .agent.discover import discover_from_url

    try:
        result = discover_from_url(url)
    except Exception as exc:
        print(f"error: discovery failed: {exc}", file=sys.stderr)
        return 1

    if dataset_id:
        result.dataset_id = dataset_id

    print(f"Discovered: {result.title}")
    print(f"  Endpoint: {result.base_url}/{result.operation}")
    print(f"  Params:   {[p.name for p in result.params]}")
    print()

    spec_yaml = result.to_spec_yaml()

    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(spec_yaml)
        print(f"Spec written to {out_path}")
    else:
        print(spec_yaml)

    return 0


def _run_monitor(*, state_file: str, add: str | None, check: bool) -> int:
    """Manage and check pending dataset applications."""
    from .agent.monitor import MonitorState, check_approval

    state_path = Path(state_file)
    state = MonitorState.load(state_path)

    if add:
        state.add(add)
        state.save(state_path)
        print(f"Added {add} to pending list")
        return 0

    if check:
        if not state.pending:
            print("No pending datasets")
            return 0

        print(f"Checking {len(state.pending)} pending dataset(s)...\n")
        changed = False
        for p in list(state.pending):
            status = check_approval(p.dataset_id)
            old_status = p.status
            p.status = status
            p.last_checked = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

            icon = "APPROVED" if status == "HEALTHY" else status
            print(f"  {p.dataset_id:40s} {old_status} -> {icon}")
            if status != old_status:
                changed = True

        if changed:
            state.save(state_path)
            print(f"\nState updated: {state_path}")
        return 0

    # Default: list pending
    if not state.pending:
        print("No pending datasets")
    else:
        print(f"Pending datasets ({len(state.pending)}):\n")
        for p in state.pending:
            print(f"  {p.dataset_id:40s} {p.status}")
    return 0


def _run_pipeline(
    dataset: str,
    *,
    kpubdata_root: str | None,
    skip_pr: bool,
) -> int:
    """Run the automated onboarding pipeline for a dataset."""
    from .agent.pipeline import run_pipeline

    root = Path(kpubdata_root) if kpubdata_root else _find_kpubdata_root()
    if root is None:
        print("error: cannot find kpubdata root. Use --kpubdata-root.", file=sys.stderr)
        return 1

    print(f"Running pipeline for {dataset} (root: {root})\n")
    result = run_pipeline(dataset, kpubdata_root=root, skip_pr=skip_pr)

    print(f"  Step reached: {result.step_reached}")
    print(f"  Success:      {result.success}")
    if result.detail:
        print(f"  Detail:       {result.detail}")
    if result.pr_url:
        print(f"  PR:           {result.pr_url}")

    return 0 if result.success else 1


def _find_kpubdata_root() -> Path | None:
    """Try to find the kpubdata repo root relative to this package."""
    # Common layout: kpubdata-builder and kpubdata are siblings
    builder_root = Path(__file__).resolve().parents[2]
    candidate = builder_root.parent / "kpubdata"
    if (candidate / "src" / "kpubdata").is_dir():
        return candidate
    return None


def dispatch(args: argparse.Namespace) -> int:
    """파싱된 argparse 결과를 실제 명령 실행 함수로 전달한다.

    매개변수:
        args: argparse가 생성한 네임스페이스.

    반환값:
        int: CLI 종료 코드.

    예시:
        >>> parser = build_parser()
        >>> dispatch(parser.parse_args(["preview"]))
        1
    """
    command = args.command
    if command == "validate":
        return _run_validate(args.spec)
    if command == "preview":
        return _run_preview(args.spec, limit=args.limit)
    if command == "build":
        return _run_build(args.spec, output_dir=args.output_dir, run_id=args.run_id)
    if command == "publish":
        return _run_publish(
            args.spec,
            target=args.target,
            destination=args.destination,
            artifacts_dir=args.artifacts_dir,
            public=args.public,
        )
    if command == "serve":
        return _run_serve(
            output_dir=args.output_dir,
            host=args.host,
            port=args.port,
            max_workers=args.max_workers,
        )
    if command == "rebuild-index":
        return _run_rebuild_index(output_dir=args.output_dir)
    if command == "prune-cancelled":
        return _run_prune_cancelled(
            output_dir=args.output_dir,
            ttl_hours=args.ttl_hours,
            apply=args.apply,
        )
    if command == "discover":
        return _run_discover(args.url, dataset_id=args.dataset_id, output=args.output)
    if command == "monitor":
        return _run_monitor(
            state_file=args.state_file, add=args.add, check=args.check,
        )
    if command == "pipeline":
        return _run_pipeline(
            args.dataset, kpubdata_root=args.kpubdata_root, skip_pr=args.skip_pr,
        )
    # 일반적인 CLI 경로로는 도달할 수 없지만(argparse가 알 수 없는 하위 명령을 거부함),
    # 프로그래밍 방식 호출자를 위한 방어적 대체 경로로 유지한다.
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 프로세스의 최상위 진입점으로 동작한다.

    매개변수:
        argv: 테스트나 프로그래밍 호출을 위한 인자 목록. None이면 sys.argv 사용.

    반환값:
        int: 운영체제에 전달할 종료 코드.

    예외:
        argparse가 발생시키는 SystemExit를 내부적으로 종료 코드로 변환한다.

    예시:
        >>> main(["--version"]) in {0, 2}
        True
    """
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        return 2
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    return dispatch(args)


__all__ = ["build_parser", "dispatch", "main"]
