"""docker-entrypoint.sh 계약 테스트 (#371).

컨테이너 진입점이 service/app.py:_is_dev_mode()와 동일한 환경변수
(``KPUBDATA_BUILDER_DEV_MODE``)를 읽는지, 그리고 fail-closed 게이트가
API 키·dev-mode 조합에 대해 올바로 동작하는지 검증한다. 예전 이름
(``KPUBDATA_BUILDER_DEV``)은 더 이상 허용되지 않아야 한다(회귀 방지).
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "docker-entrypoint.sh"

# 모듈이 sh 없는 환경에서는 건너뛴다(이 프로젝트 CI는 Linux).
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="entrypoint is a POSIX sh script")

# 진입점이 게이트 통과 후 exec 하는 ``kpubdata-builder``를 가로채기 위한 더미.
_STUB_SCRIPT = "#!/bin/sh\nexit 0\n"


def _stub_bin_dir(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "kpubdata-builder"
    stub.write_text(_STUB_SCRIPT)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _run_entrypoint(env: dict[str, str], bin_dir: Path) -> subprocess.CompletedProcess[bytes]:
    # 테스트 러너 환경의 키 변수 누수를 막기 위해 제어 대상 변수는 한 번 비우고 다시 주입.
    full_env = {k: v for k, v in os.environ.items() if k not in _CONTROLLED_ENVS}
    full_env["PATH"] = f"{bin_dir}:{full_env.get('PATH', '')}"
    for key, value in env.items():
        if value:
            full_env[key] = value
    return subprocess.run(["sh", str(ENTRYPOINT)], env=full_env, capture_output=True, check=False)


_CONTROLLED_ENVS = {
    "KPUBDATA_BUILDER_API_KEY",
    "KPUBDATA_BUILDER_DEV_MODE",
    "KPUBDATA_BUILDER_DEV",
}


@pytest.fixture()
def bin_dir(tmp_path: Path) -> Path:
    return _stub_bin_dir(tmp_path)


class TestEntrypointDevModeContract:
    """진입점 게이트가 app.py의 dev-mode 시맨틱과 일치하는지 검증 (#371)."""

    def test_no_key_no_dev_mode_rejected(self, bin_dir: Path) -> None:
        # fail-closed: 둘 다 없으면 기동 거부 (exit 1).
        result = _run_entrypoint({}, bin_dir)
        assert result.returncode == 1, result.stderr
        assert b"KPUBDATA_BUILDER_API_KEY is required" in result.stderr

    def test_api_key_set_proceeds(self, bin_dir: Path) -> None:
        result = _run_entrypoint({"KPUBDATA_BUILDER_API_KEY": "secret"}, bin_dir)
        assert result.returncode == 0, result.stderr

    def test_dev_mode_1_allows_no_key(self, bin_dir: Path) -> None:
        result = _run_entrypoint({"KPUBDATA_BUILDER_DEV_MODE": "1"}, bin_dir)
        assert result.returncode == 0, result.stderr

    def test_dev_mode_true_case_insensitive(self, bin_dir: Path) -> None:
        # app.py:_is_dev_mode()가 'true'/'1'을 대소문자 무관으로 받으므로 진입점도 동일.
        for value in ("true", "TRUE", "True"):
            result = _run_entrypoint({"KPUBDATA_BUILDER_DEV_MODE": value}, bin_dir)
            assert result.returncode == 0, f"DEV_MODE={value!r} should allow startup"

    def test_dev_mode_explicit_false_rejected(self, bin_dir: Path) -> None:
        result = _run_entrypoint({"KPUBDATA_BUILDER_DEV_MODE": "0"}, bin_dir)
        assert result.returncode == 1, result.stderr

    def test_legacy_DEV_name_not_recognized(self, bin_dir: Path) -> None:
        # 회귀 방지: 예전 이름 KPUBDATA_BUILDER_DEV 는 더 이상 효과가 없어야 한다.
        result = _run_entrypoint({"KPUBDATA_BUILDER_DEV": "1"}, bin_dir)
        assert result.returncode == 1, "legacy KPUBDATA_BUILDER_DEV must NOT bypass fail-closed"
