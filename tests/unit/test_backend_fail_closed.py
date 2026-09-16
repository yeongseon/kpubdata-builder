"""상태 백엔드 fail-closed 게이트 단위 테스트 (#587, ADR 0016).

``serve()`` 가 기동 시 호출하는 ``validate_storage_config()`` 와 백엔드 선택자
(``storage_backend``/``cubrid_url``)의 fail-closed 계약을 검증한다. 이 모듈은
sqlalchemy 를 import 하지 않는다 — 기본(sqlite) 경로가 optional 의존성 없이
동작한다는 것 자체가 계약이므로, 기본 dev 환경(py3.10~3.13) 전부에서 실행된다.

드라이버(``sqlalchemy-cubrid``)는 dev extra 를 py3.12+ 마커로만 끌어오므로,
미설치 환경 분기는 skipif 로 게이팅한다. 실 서버 연결 검증은
``tests/cubrid/test_cubrid_fail_closed.py`` (전용 CI 잡)에서 수행한다.
"""

from __future__ import annotations

import importlib.util

import pytest

from kpubdata_builder.store.backend import (
    dispose_engine,
    storage_backend,
    validate_storage_config,
)

_DRIVER_INSTALLED = importlib.util.find_spec("sqlalchemy_cubrid") is not None

_BAD_URL = "cubrid+pycubrid://dba:@127.0.0.1:33000/kpubdata?charset=utf8"


def test_storage_backend_defaults_to_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KPUBDATA_BUILDER_STORAGE_BACKEND", raising=False)
    assert storage_backend() == "sqlite"


def test_storage_backend_accepts_explicit_sqlite_and_cubrid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_STORAGE_BACKEND", "sqlite")
    assert storage_backend() == "sqlite"
    monkeypatch.setenv("KPUBDATA_BUILDER_STORAGE_BACKEND", "cubrid")
    assert storage_backend() == "cubrid"


def test_storage_backend_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_STORAGE_BACKEND", "  CUBRID ")
    assert storage_backend() == "cubrid"


def test_storage_backend_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_STORAGE_BACKEND", "mysql")
    with pytest.raises(RuntimeError, match="must be 'sqlite' or 'cubrid'"):
        storage_backend()


def test_validate_is_noop_for_default_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KPUBDATA_BUILDER_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("KPUBDATA_BUILDER_CUBRID_URL", raising=False)
    assert validate_storage_config() is None


def test_validate_fails_closed_without_cubrid_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_STORAGE_BACKEND", "cubrid")
    monkeypatch.delenv("KPUBDATA_BUILDER_CUBRID_URL", raising=False)
    with pytest.raises(RuntimeError, match="KPUBDATA_BUILDER_CUBRID_URL"):
        validate_storage_config()


@pytest.mark.skipif(_DRIVER_INSTALLED, reason="sqlalchemy-cubrid 가 설치된 환경")
def test_validate_fails_closed_without_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_STORAGE_BACKEND", "cubrid")
    monkeypatch.setenv("KPUBDATA_BUILDER_CUBRID_URL", _BAD_URL)
    with pytest.raises(RuntimeError, match="sqlalchemy-cubrid is not installed"):
        validate_storage_config()


def test_engine_singleton_and_dispose(monkeypatch: pytest.MonkeyPatch) -> None:
    """전역 Engine 은 프로세스 내 단일 인스턴스고 dispose 후 재생성된다.

    sqlite 파일 URL 로 드라이버 없이 검증한다 — get_engine() 이 sqlalchemy 를
    lazy import 하는 경로만 확인하면 되고, CUBRID 드라이버는 불필요하다.
    """
    sqlalchemy = pytest.importorskip("sqlalchemy")
    monkeypatch.setenv(
        "KPUBDATA_BUILDER_CUBRID_URL",
        "sqlite:///:memory:",  # dialect 만 필요
    )
    try:
        from kpubdata_builder.store.backend import get_engine

        engine = get_engine()
        assert get_engine() is engine
        with engine.connect() as conn:
            assert conn.execute(sqlalchemy.text("SELECT 1")).scalar() == 1
        dispose_engine()
        assert get_engine() is not engine
        dispose_engine()
    finally:
        dispose_engine()
