"""kpubdata-builder용 명령줄 진입점.

이 모듈은 argparse 기반 CLI를 구성하고, validate/build 명령과 향후 예약된
preview 명령의 진입점을 제공한다.

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
from .errors import SpecLoadError, ValidationError
from .pipeline import run_build
from .spec import load_spec
from .spec.validator import validate_spec
from .stages.bronze.build import SourceClient

_RESERVED_COMMANDS: frozenset[str] = frozenset({"preview"})


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
        help="Preview a BuildSpec execution (reserved; not implemented yet).",
    )
    preview_cmd.add_argument("spec", nargs="?", help="Path to the BuildSpec YAML file.")

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


def _run_reserved(command: str) -> int:
    """예약된 미구현 명령에 대한 공통 오류 메시지를 출력한다.

    매개변수:
        command: 사용자가 요청한 하위 명령 이름.

    반환값:
        int: 항상 1.
    """
    print(
        f"error: '{command}' is not implemented yet (preview command pending — see issue #3)",
        file=sys.stderr,
    )
    return 1


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
    if command == "build":
        return _run_build(args.spec, output_dir=args.output_dir, run_id=args.run_id)
    if command in _RESERVED_COMMANDS:
        return _run_reserved(command)
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
