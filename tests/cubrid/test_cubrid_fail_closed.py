"""serve 기동 fail-closed 게이트의 실 드라이버/실 서버 검증 (#587, ADR 0016).

``validate_storage_config()`` 는 URL·드라이버 확인을 넘어 **커넥션을 실제로 한 번
연다**. 설정만 맞고 서버가 내려가 있으면 기동은 성공한 것처럼 보이고 첫 요청에서야
터지는데, 그 경로의 쓰기는 best-effort 로 삼켜지므로 상태가 조용히 유실된다.

``tests/unit/test_storage_backend.py`` 가 연결 이전 분기(백엔드 선택·URL·드라이버)를
덮고, 이 파일은 그 뒤 — 실 드라이버가 깔린 환경에서만 확인 가능한 부분을 맡는다.
그래서 cubrid 마커로 기본 스위트에서 제외되며 전용 CI 잡(``cubrid.yml``)에서 돈다.

- **연결 거부**는 서버가 없어도 검증된다(닫힌 포트 → 즉시 실패). 로컬에서도 돈다.
- **연결 성공**은 실 서버가 있어야 하므로 URL 이 없으면 skip 한다. 단
  ``KPUBDATA_BUILDER_REQUIRE_REAL_CUBRID=1`` (전용 CI 잡이 켠다)이면 skip 대신
  실패한다 — URL 주입이 빠졌는데 초록으로 지나가는 일을 막는다.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

pytest.importorskip("sqlalchemy")

from kpubdata_builder.store.backend import (  # noqa: E402
    dispose_engine,
    get_engine,
    validate_storage_config,
)

pytestmark = pytest.mark.cubrid

_BACKEND_ENV = "KPUBDATA_BUILDER_STORAGE_BACKEND"
_URL_ENV = "KPUBDATA_BUILDER_CUBRID_URL"
_REQUIRE_REAL_ENV = "KPUBDATA_BUILDER_REQUIRE_REAL_CUBRID"

_DRIVER_INSTALLED = importlib.util.find_spec("sqlalchemy_cubrid") is not None

# 포트 1 은 CUBRID broker 포트가 아니므로 연결이 즉시 거부된다(타임아웃 대기 없음).
_UNREACHABLE_URL = "cubrid+pycubrid://dba:@127.0.0.1:1/kpubdata?charset=utf8"


def _require_real_cubrid() -> bool:
    """전용 CI 잡인지 여부. 참이면 URL 없는 skip 을 금지한다 (#587)."""
    return os.environ.get(_REQUIRE_REAL_ENV, "").strip().lower() in ("1", "true", "yes")


@pytest.fixture(autouse=True)
def _dispose_global_engine():  # type: ignore[no-untyped-def]
    """전역 Engine 은 프로세스 단위라 테스트 간에 새어 나간다 — 앞뒤로 폐기한다."""
    dispose_engine()
    yield
    dispose_engine()


@pytest.mark.skipif(not _DRIVER_INSTALLED, reason="sqlalchemy-cubrid 미설치")
def test_startup_accepts_a_reachable_server(monkeypatch: pytest.MonkeyPatch) -> None:
    url = os.environ.get(_URL_ENV, "").strip()
    if not url:
        if _require_real_cubrid():
            raise AssertionError(
                f"{_REQUIRE_REAL_ENV} is set but {_URL_ENV} is empty — this run would skip "
                "the only check that proves the startup gate can reach a real CUBRID server."
            )
        pytest.skip(f"{_URL_ENV} 미설정 — 실 서버 검증은 전용 CI 잡에서 수행")
    monkeypatch.setenv(_BACKEND_ENV, "cubrid")

    validate_storage_config()

    # 게이트가 연 커넥션은 반납되지만 Engine 은 살아 있어야 한다 — serve 가 이어서
    # 쓰는 바로 그 풀이다.
    assert get_engine() is get_engine()


@pytest.mark.skipif(not _DRIVER_INSTALLED, reason="sqlalchemy-cubrid 미설치")
def test_startup_refuses_an_unreachable_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """URL·드라이버가 모두 유효해도 서버가 없으면 기동을 거부한다.

    이 테스트가 곧 회귀 방지선이다: 연결 확인을 빼면 여기서 아무 예외도 안 난다.
    """
    monkeypatch.setenv(_BACKEND_ENV, "cubrid")
    monkeypatch.setenv(_URL_ENV, _UNREACHABLE_URL)

    with pytest.raises(RuntimeError, match="not reachable"):
        validate_storage_config()

    # 실패한 Engine 을 폐기하므로 재시도도 같은 오류로 깔끔하게 실패한다 — 죽은
    # 풀을 물고 다른 오류로 변질되지 않는다.
    with pytest.raises(RuntimeError, match="not reachable"):
        validate_storage_config()
