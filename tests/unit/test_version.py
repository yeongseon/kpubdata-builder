"""버전 메타데이터가 한쪽으로 갈라지지 않는지 지키는 회귀 테스트 (#592).

`pyproject.toml` 의 `version` 이 정본이고, `kpubdata_builder.__version__` 은 설치된
배포판 메타데이터에서 파생된다. CHANGELOG 최상단 절은 그 버전 라인을 서술해야 한다 —
이 셋이 어긋나면 배포 산출물(GHCR 이미지 태그·`--version` 출력·manifest 의
`builder_version`)이 문서와 다른 버전을 주장하게 된다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kpubdata_builder import __version__

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _pyproject_version() -> str:
    # tomllib 은 3.11+ 이고 이 프로젝트는 3.10 을 지원하므로 정규식으로 읽는다.
    text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, flags=re.MULTILINE)
    assert match is not None, "pyproject.toml 에 [project] version 이 없다"
    return match.group(1)


def _changelog_latest_version() -> str:
    text = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(r"^## v(\d+\.\d+)", text, flags=re.MULTILINE)
    assert match is not None, "CHANGELOG.md 에 '## vX.Y' 절이 없다"
    return match.group(1)


def test_package_version_matches_pyproject() -> None:
    """``__version__`` 은 하드코딩이 아니라 배포판 메타데이터에서 와야 한다."""
    if __version__ == "0.0.0+unknown":
        pytest.skip("패키지가 설치되지 않은 소스 트리 — 메타데이터를 읽을 수 없다")
    expected = _pyproject_version()
    assert __version__ == expected, (
        f"설치된 배포판 메타데이터는 {__version__}, pyproject.toml 은 {expected} 다. "
        "버전을 올린 뒤 `uv sync` 로 editable 설치 메타데이터를 갱신하라."
    )


def test_changelog_head_matches_package_version_line() -> None:
    """CHANGELOG 최상단 절과 패키지 버전의 major.minor 가 같아야 한다."""
    major_minor = ".".join(_pyproject_version().split(".")[:2])
    assert _changelog_latest_version() == major_minor
