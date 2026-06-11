"""kpubdata-builder용 명령줄 진입점.

이 모듈은 argparse 기반 CLI를 구성하고, validate/preview/build/publish 명령의
진입점을 제공한다.

주요 함수:
    - build_parser: 하위 명령을 포함한 ArgumentParser 구성
    - dispatch: 파싱된 명령을 실제 실행 함수로 분기
    - main: CLI 프로세스용 최상위 진입점
"""

from __future__ import annotations

import argparse
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

    return parser


def _create_client() -> SourceClient:
    """kpubdata 클라이언트를 환경설정으로 생성한다.

    테스트에서 monkeypatch로 대체할 수 있도록 별도 함수로 분리한다. 실제
    네트워크 호출은 build 실행(run_build) 시점에만 발생한다.
    """
    from kpubdata import Client

    return cast(SourceClient, Client.from_env())


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
