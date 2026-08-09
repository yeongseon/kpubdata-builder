"""README 환경변수 ↔ 코드 상수 교집합 검사 테스트 (#424).

README에 등장하는 환경변수명이 코드의 상수와 정합하는지 검증한다.
코드에 있으면서 README에 없으면 신규 기여자가 발견하지 못하고,
README에 있으면서 코드에 없으면 잘못된 안내가 된다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_CODE_ENV_SOURCES = [
    _REPO_ROOT / "src" / "kpubdata_builder" / "service" / "app.py",
    _REPO_ROOT / "src" / "kpubdata_builder" / "service" / "http.py",
    _REPO_ROOT / "src" / "kpubdata_builder" / "service" / "auth.py",
    _REPO_ROOT / "src" / "kpubdata_builder" / "cli.py",
    _REPO_ROOT / "docker-entrypoint.sh",
    _REPO_ROOT / "Dockerfile",
]

_README = _REPO_ROOT / "README.md"

_ENV_PATTERN = re.compile(r"KPUBDATA_BUILDER_[A-Z_]+")
_QUOTED_PATTERN = re.compile(r'"(KPUBDATA_BUILDER_[A-Z_]+)"')


def _readme_env_vars() -> set[str]:
    text = _README.read_text(encoding="utf-8")
    return set(_ENV_PATTERN.findall(text))


def _code_env_vars() -> set[str]:
    found: set[str] = set()
    for source in _CODE_ENV_SOURCES:
        if not source.exists():
            continue
        text = source.read_text(encoding="utf-8")
        found.update(_QUOTED_PATTERN.findall(text))
        found.update(_ENV_PATTERN.findall(text))
    return found


class TestEnvVarContract:
    """README 환경변수 표와 코드 상수의 정합성을 검증한다 (#424)."""

    def test_all_code_env_vars_are_in_readme(self) -> None:
        code_vars = _code_env_vars()
        readme_vars = _readme_env_vars()
        missing = code_vars - readme_vars
        assert not missing, (
            f"코드에 있지만 README에 없는 환경변수 (신규 기여자가 발견 불가): {sorted(missing)}"
        )

    def test_all_readme_env_vars_exist_in_code(self) -> None:
        code_vars = _code_env_vars()
        readme_vars = _readme_env_vars()
        stale = readme_vars - code_vars
        if stale:
            pytest.fail(
                f"README에 있지만 코드에 없는 환경변수 (잘못된 안내): {sorted(stale)}"
            )
