"""상태 백엔드 선택 (ADR 0016).

기본은 sqlite/local FS 다(무외부의존·결정성, AGENTS.md). 환경변수
``KPUBDATA_BUILDER_STORAGE_BACKEND=cubrid`` 이면 CUBRID(SQLAlchemy)로 전환한다.

핵심 규칙:
    - ``sqlalchemy`` import 는 반드시 이 모듈의 cubrid 분기 *안에서만* 이뤄진다.
      기본(sqlite) 경로는 SQLAlchemy 를 import 하지 않으므로 optional 의존성이
      없어도 서비스가 동작한다.
    - CUBRID 컴포넌트(BuildIndex/Credential/ArtifactStore)는 **프로세스 전역 단일
      Engine** 을 공유한다. Engine 은 커넥션 풀을 관리하므로, 멀티스레드(#334 async
      job 등)에서 스레드마다 raw connection 을 재사용하지 않고 연산마다 짧은
      커넥션을 빌린다(``with engine.begin()``).
    - ``pool_pre_ping=True`` 로 stale connection 을 자동 감지한다(CUBRID 재시작·유휴
      타임아웃 대비).
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from sqlalchemy import Engine

_BACKEND_ENV = "KPUBDATA_BUILDER_STORAGE_BACKEND"
_CUBRID_URL_ENV = "KPUBDATA_BUILDER_CUBRID_URL"

StorageBackend = Literal["sqlite", "cubrid"]

_engine_lock = threading.Lock()
_engine: Engine | None = None


def storage_backend() -> StorageBackend:
    """선택된 상태 백엔드. 미설정/빈 값이면 ``sqlite`` (기본)."""
    raw = os.environ.get(_BACKEND_ENV, "").strip().lower()
    if raw in ("", "sqlite"):
        return "sqlite"
    if raw == "cubrid":
        return "cubrid"
    raise RuntimeError(f"{_BACKEND_ENV} must be 'sqlite' or 'cubrid', got {raw!r}")


def cubrid_url() -> str:
    """CUBRID SQLAlchemy URL. cubrid 백엔드인데 미설정이면 fail-closed."""
    url = os.environ.get(_CUBRID_URL_ENV, "").strip()
    if not url:
        raise RuntimeError(
            f"{_BACKEND_ENV}=cubrid requires {_CUBRID_URL_ENV} "
            "(SQLAlchemy URL, e.g. cubrid+pycubrid://user:pass@host:33000/db?charset=utf8)"
        )
    return url


def get_engine() -> Engine:
    """프로세스 전역 단일 SQLAlchemy ``Engine`` (지연 생성, thread-safe).

    ``sqlalchemy`` import 는 이 함수 안에서만 이뤄진다 — sqlite 기본 경로는 이
    함수를 호출하지 않으므로 SQLAlchemy 의존이 없다.
    """
    global _engine
    with _engine_lock:
        if _engine is None:
            from sqlalchemy import create_engine

            _engine = create_engine(cubrid_url(), pool_pre_ping=True, future=True)
        return _engine


def validate_storage_config() -> None:
    """serve 시작 시 호출 (fail-fast, ADR 0016).

    - sqlite 백엔드 → no-op.
    - cubrid 백엔드 → URL 미설정, ``sqlalchemy-cubrid`` 미설치, 실 연결 실패 중
      하나라도 해당하면 기동 거부. (인덱스/매니페스트 쓰기 실패는 런타임에
      best-effort 로 삼키지만, 기동 시 설정 오류는 조기에 드러내야 한다.)
    """
    if storage_backend() != "cubrid":
        return
    cubrid_url()
    try:
        import sqlalchemy_cubrid  # noqa: F401
    except ImportError as exc:  # pragma: no cover - extra 미설치 환경
        raise RuntimeError(
            "KPUBDATA_BUILDER_STORAGE_BACKEND=cubrid but sqlalchemy-cubrid is not "
            "installed; install with: uv sync --extra cubrid (requires Python 3.12+)."
        ) from exc
    # 실 연결 확인(#587): URL·드라이버가 있어도 서버가 내려가 있으면 첫 요청까지
    # 실패가 미뤄진다. serve 는 기동 시점에 실제 커넥션을 한 번 열어 fail-closed 한다.
    # 검증용 커넥션은 즉시 닫으며, 풀은 이후 정상 요청에 재사용된다.
    try:
        with get_engine().connect():
            pass
    except Exception as exc:
        dispose_engine()
        raise RuntimeError(
            f"{_BACKEND_ENV}=cubrid but the CUBRID server is not reachable "
            f"({_CUBRID_URL_ENV}); refusing to start. Underlying error: {exc}"
        ) from exc


def dispose_engine() -> None:
    """전역 Engine 을 폐기한다 (프로세스 종료·테스트 정리용)."""
    global _engine
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
            _engine = None


__all__ = [
    "StorageBackend",
    "cubrid_url",
    "dispose_engine",
    "get_engine",
    "storage_backend",
    "validate_storage_config",
]
