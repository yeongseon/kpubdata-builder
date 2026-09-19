"""상태 백엔드 선택, CUBRID URL 정규화, serve 기동 게이트 (ADR 0016).

여기서는 SQLAlchemy 도 CUBRID 도 필요 없다 — 백엔드 선택과 URL 계약, 그리고
``validate_storage_config()`` 가 **실 연결에 도달하기 전에** 거부하는 분기만
검증한다. 연결까지 가는 경로는 ``tests/cubrid/test_cubrid_fail_closed.py``.
"""

from __future__ import annotations

import logging

import pytest

from kpubdata_builder.store.backend import (
    cubrid_url,
    normalize_cubrid_url,
    storage_backend,
    validate_storage_config,
)

_BACKEND_ENV = "KPUBDATA_BUILDER_STORAGE_BACKEND"
_URL_ENV = "KPUBDATA_BUILDER_CUBRID_URL"
_TAIL = "dba:@127.0.0.1:33000/kpubdata?charset=utf8"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (_BACKEND_ENV, _URL_ENV):
        monkeypatch.delenv(key, raising=False)


class TestStorageBackendSelection:
    def test_defaults_to_sqlite(self) -> None:
        assert storage_backend() == "sqlite"

    @pytest.mark.parametrize("value", ["sqlite", "cubrid", "CUBRID", " cubrid "])
    def test_accepts_known_backends(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_BACKEND_ENV, value)
        assert storage_backend() == value.strip().lower()

    def test_rejects_unknown_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_BACKEND_ENV, "postgres")
        with pytest.raises(RuntimeError, match="must be 'sqlite' or 'cubrid'"):
            storage_backend()


class TestCubridUrlDriver:
    """URL 은 항상 pycubrid 드라이버로 해석돼야 한다 (ADR 0016).

    sqlalchemy-cubrid 는 `cubrid`/`cubrid.cubrid`/`cubrid.cubriddb` 를 legacy
    C-extension(`CUBRIDdb`) dialect 로, `cubrid.pycubrid` 만 순수 파이썬 드라이버로
    등록한다. `[cubrid]` extra 는 pycubrid 만 설치하므로 나머지 경로는 연결 시점에
    ImportError 로 죽는다 — 기동 시 걸러야 한다.
    """

    def test_pycubrid_url_passes_through(self) -> None:
        url = f"cubrid+pycubrid://{_TAIL}"
        assert normalize_cubrid_url(url) == url

    def test_bare_cubrid_url_is_normalized(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            assert normalize_cubrid_url(f"cubrid://{_TAIL}") == f"cubrid+pycubrid://{_TAIL}"
        # 조용히 바꾸지 않는다 — 설정이 보정됐다는 사실이 로그에 남아야 한다.
        assert any("pycubrid" in record.getMessage() for record in caplog.records)

    @pytest.mark.parametrize("driver", ["cubriddb", "cubrid"])
    def test_rejects_c_extension_drivers(self, driver: str) -> None:
        with pytest.raises(RuntimeError, match="pycubrid"):
            normalize_cubrid_url(f"cubrid+{driver}://{_TAIL}")

    def test_rejects_async_driver(self) -> None:
        # Engine 은 동기다 — async dialect 는 첫 연결에서야 터진다.
        with pytest.raises(RuntimeError, match="synchronous"):
            normalize_cubrid_url(f"cubrid+aiopycubrid://{_TAIL}")

    def test_rejects_other_dialects(self) -> None:
        with pytest.raises(RuntimeError, match="dialect"):
            normalize_cubrid_url("postgresql+psycopg://user:pass@host/db")

    def test_rejects_url_without_scheme(self) -> None:
        with pytest.raises(RuntimeError, match="SQLAlchemy URL"):
            normalize_cubrid_url("127.0.0.1:33000/kpubdata")


class TestCubridUrlFromEnv:
    def test_missing_url_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_BACKEND_ENV, "cubrid")
        with pytest.raises(RuntimeError, match=_URL_ENV):
            cubrid_url()

    def test_env_url_is_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_BACKEND_ENV, "cubrid")
        monkeypatch.setenv(_URL_ENV, f"  cubrid://{_TAIL}  ")
        assert cubrid_url() == f"cubrid+pycubrid://{_TAIL}"


class TestValidateStorageConfig:
    """``serve()`` 기동 게이트의 계약 (#587, ADR 0016).

    여기서 검증하는 분기는 전부 **실 연결 이전**에 끝난다 — 기본 dev 환경
    (sqlalchemy 미설치 가능)에서도 돌아야 하므로, 연결까지 가는 경로는
    ``tests/cubrid/test_cubrid_fail_closed.py`` 로 분리했다.
    """

    def test_is_noop_for_the_default_backend(self) -> None:
        # sqlite 기본 경로는 optional 의존성 없이 통과해야 한다(무외부의존 계약).
        assert validate_storage_config() is None

    def test_is_noop_for_explicit_sqlite(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_BACKEND_ENV, "sqlite")
        assert validate_storage_config() is None

    def test_refuses_to_start_without_a_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_BACKEND_ENV, "cubrid")
        with pytest.raises(RuntimeError, match=_URL_ENV):
            validate_storage_config()

    @pytest.mark.parametrize("driver", ["cubriddb", "cubrid", "aiopycubrid"])
    def test_refuses_to_start_on_an_unsupported_driver(
        self, driver: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 드라이버 거부는 URL 정규화 단계에서 끝나므로 서버도 sqlalchemy 도 필요 없다.
        monkeypatch.setenv(_BACKEND_ENV, "cubrid")
        monkeypatch.setenv(_URL_ENV, f"cubrid+{driver}://{_TAIL}")
        with pytest.raises(RuntimeError):
            validate_storage_config()

    def test_refuses_to_start_on_an_unknown_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_BACKEND_ENV, "postgres")
        with pytest.raises(RuntimeError, match="must be 'sqlite' or 'cubrid'"):
            validate_storage_config()
