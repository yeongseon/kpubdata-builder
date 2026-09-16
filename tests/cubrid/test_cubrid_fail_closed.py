"""serve 시작 fail-closed 게이트의 실 드라이버/실 서버 검증 (#587, ADR 0016).

``validate_storage_config()`` 가 URL·드라이버 확인을 넘어 **실제 연결**까지
확인하는지를 검증한다. 드라이버(``sqlalchemy-cubrid``)가 필요하므로 cubrid
마커로 기본 스위트에서 제외되며, 전용 CI 잡(``cubrid.yml``)에서 실 CUBRID 를
대상으로 실행된다.

- 실 서버가 필요한 검증(URL 주입 실행)은 ``KPUBDATA_BUILDER_CUBRID_URL`` 이
  없으면 skip 한다 — 로컬 in-memory 폴백 환경에서도 스위트가 깨지지 않는다.
- 연결 거부 검증은 서버 없이도 가능하다(닫힌 포트 → 즉시 실패).
"""

from __future__ import annotations

import importlib.util
import os

import pytest

pytest.importorskip("sqlalchemy")

from kpubdata_builder.store.backend import (  # noqa: E402
    dispose_engine,
    get_engine,
    storage_backend,
    validate_storage_config,
)

pytestmark = pytest.mark.cubrid

_DRIVER_INSTALLED = importlib.util.find_spec("sqlalchemy_cubrid") is not None

_UNREACHABLE_URL = "cubrid+pycubrid://dba:@127.0.0.1:1/kpubdata?charset=utf8"


@pytest.mark.skipif(not _DRIVER_INSTALLED, reason="sqlalchemy-cubrid 미설치")
def test_validate_passes_with_real_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    url = os.environ.get("KPUBDATA_BUILDER_CUBRID_URL")
    if not url:
        pytest.skip("KPUBDATA_BUILDER_CUBRID_URL 미설정 — 실 서버 검증은 전용 CI 잡에서 수행")
    monkeypatch.setenv("KPUBDATA_BUILDER_STORAGE_BACKEND", "cubrid")
    try:
        # URL·드라이버·실 연결까지 모두 통과해야 기동 가능 상태다.
        validate_storage_config()
        assert storage_backend() == "cubrid"
        # 검증이 만든 전역 Engine 은 이후 컴포넌트가 재사용한다(단일 인스턴스).
        assert get_engine() is get_engine()
    finally:
        dispose_engine()


@pytest.mark.skipif(not _DRIVER_INSTALLED, reason="sqlalchemy-cubrid 미설치")
def test_validate_fails_closed_on_unreachable_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_STORAGE_BACKEND", "cubrid")
    monkeypatch.setenv("KPUBDATA_BUILDER_CUBRID_URL", _UNREACHABLE_URL)
    try:
        with pytest.raises(RuntimeError, match="not reachable"):
            validate_storage_config()
        # 실패 시 전역 Engine 을 폐기하므로, 재검증도 같은 오류로 깔끔하게 실패한다.
        with pytest.raises(RuntimeError, match="not reachable"):
            validate_storage_config()
    finally:
        dispose_engine()
