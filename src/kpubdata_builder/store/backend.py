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

import logging
import os
import threading
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from sqlalchemy import Engine

_BACKEND_ENV = "KPUBDATA_BUILDER_STORAGE_BACKEND"
_CUBRID_URL_ENV = "KPUBDATA_BUILDER_CUBRID_URL"

# ADR 0016 이 고른 드라이버. sqlalchemy-cubrid 는 dialect 를 네 가지로 등록하는데
# (`cubrid`, `cubrid.cubrid`, `cubrid.cubriddb`, `cubrid.pycubrid`), 앞의 셋은 모두
# legacy C-extension(CUBRID-Python, import 이름 `CUBRIDdb`)을 쓰고 순수 파이썬
# 드라이버는 `pycubrid` 하나뿐이다. `[cubrid]` extra 는 pycubrid 만 설치한다.
_CUBRID_DRIVER = "pycubrid"
_CUBRID_DIALECT = "cubrid"
_CANONICAL_SCHEME = f"{_CUBRID_DIALECT}+{_CUBRID_DRIVER}"
# 비동기 dialect. Engine 은 동기라 여기서는 쓸 수 없다.
_ASYNC_DRIVER = "aiopycubrid"

_logger = logging.getLogger(__name__)

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


def normalize_cubrid_url(url: str) -> str:
    """URL 이 pycubrid 드라이버를 쓰도록 강제한다 (ADR 0016).

    드라이버를 생략한 ``cubrid://`` 는 SQLAlchemy 가 기본 dialect(= C-extension
    기반)로 해석해 연결 시점에 ``ImportError: Could not import CUBRIDdb`` 로 죽는다.
    기동 시 조용히 통과했다가 첫 쿼리에서 터지므로, 여기서 정규화해 그 경로를 없앤다.

    - ``cubrid+pycubrid://`` — 그대로 통과.
    - ``cubrid://`` — ``cubrid+pycubrid://`` 로 정규화한다(경고 로그).
    - ``cubrid+cubriddb://`` / ``cubrid+cubrid://`` — 거부. C-extension 드라이버는
      ``[cubrid]`` extra 가 설치하지 않으며 이 백엔드에서 검증된 적도 없다.
    - ``cubrid+aiopycubrid://`` — 거부. 동기 ``Engine`` 과 맞지 않는다.
    - 그 밖의 scheme — 거부(오타/다른 DB URL 주입 방지).
    """
    scheme, separator, remainder = url.partition("://")
    if not separator:
        raise RuntimeError(
            f"{_CUBRID_URL_ENV} must be a SQLAlchemy URL like "
            f"{_CANONICAL_SCHEME}://user:pass@host:33000/db?charset=utf8, got {url!r}"
        )
    dialect, _, driver = scheme.partition("+")
    if dialect != _CUBRID_DIALECT:
        raise RuntimeError(
            f"{_CUBRID_URL_ENV} must use the '{_CUBRID_DIALECT}' dialect "
            f"(e.g. {_CANONICAL_SCHEME}://...), got scheme {scheme!r}"
        )
    if driver == _CUBRID_DRIVER:
        return url
    if not driver:
        # 드라이버를 생략한 URL 은 조용히 고치되, 설정이 바뀐 사실은 남긴다.
        _logger.warning(
            "%s omits the driver; using %s (a bare cubrid:// URL resolves to the "
            "legacy CUBRIDdb C-extension, which this build does not install).",
            _CUBRID_URL_ENV,
            _CANONICAL_SCHEME,
        )
        return f"{_CANONICAL_SCHEME}://{remainder}"
    if driver == _ASYNC_DRIVER:
        raise RuntimeError(
            f"{_CUBRID_URL_ENV} uses the async driver {driver!r}, but this backend "
            f"runs on a synchronous SQLAlchemy Engine; use {_CANONICAL_SCHEME}:// instead."
        )
    raise RuntimeError(
        f"{_CUBRID_URL_ENV} uses driver {driver!r}, which is backed by the legacy "
        "CUBRID-Python C-extension. The [cubrid] extra installs the pure-python "
        f"pycubrid driver only (ADR 0016) — use {_CANONICAL_SCHEME}:// instead."
    )


def cubrid_url() -> str:
    """CUBRID SQLAlchemy URL. cubrid 백엔드인데 미설정이면 fail-closed.

    반환값은 항상 pycubrid 드라이버로 정규화된다(``normalize_cubrid_url``).
    """
    url = os.environ.get(_CUBRID_URL_ENV, "").strip()
    if not url:
        raise RuntimeError(
            f"{_BACKEND_ENV}=cubrid requires {_CUBRID_URL_ENV} "
            f"(SQLAlchemy URL, e.g. {_CANONICAL_SCHEME}://user:pass@host:33000/db?charset=utf8)"
        )
    return normalize_cubrid_url(url)


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
    - cubrid 백엔드 → URL 미설정/드라이버 불일치, ``sqlalchemy-cubrid``·``pycubrid``
      미설치, **실 연결 실패** 중 하나라도 해당하면 기동 거부. (인덱스/매니페스트
      쓰기 실패는 런타임에 best-effort 로 삼키지만, 기동 시 설정 오류는 조기에
      드러내야 한다.)

    설정만 맞고 서버가 내려가 있으면 기동은 성공한 것처럼 보이고 첫 요청에서야
    터진다 — 그때는 이미 쓰기가 best-effort 로 삼켜지는 경로라 상태가 조용히
    유실된다. 그래서 여기서 커넥션을 한 번 실제로 연다 (#587).
    """
    if storage_backend() != "cubrid":
        return
    cubrid_url()
    try:
        import sqlalchemy_cubrid  # noqa: F401
    except ImportError as exc:  # pragma: no cover - extra 미설치 환경
        raise RuntimeError(
            "KPUBDATA_BUILDER_STORAGE_BACKEND=cubrid but sqlalchemy-cubrid is not "
            "installed; install with: uv sync --extra cubrid."
        ) from exc
    try:
        # URL 을 정규화해도 드라이버 자체가 없으면 첫 쿼리에서 터진다 — 여기서 잡는다.
        import pycubrid  # noqa: F401
    except ImportError as exc:  # pragma: no cover - extra 미설치 환경
        raise RuntimeError(
            "KPUBDATA_BUILDER_STORAGE_BACKEND=cubrid but the pycubrid driver is not "
            "installed; install with: uv sync --extra cubrid (sqlalchemy-cubrid alone "
            "does not pull a driver — ADR 0016 uses sqlalchemy-cubrid[pycubrid])."
        ) from exc
    try:
        # 검증용 커넥션은 즉시 반납하지만 Engine 은 남긴다 — serve 가 이어서 쓰는
        # 그 풀이므로, 기동 직후 첫 요청이 커넥션을 새로 맺지 않아도 된다.
        with get_engine().connect():
            pass
    except Exception as exc:
        # 실패한 Engine 을 남겨두면 다음 호출이 같은 죽은 풀을 되쓴다. 폐기해서
        # 재시도가 URL 부터 다시 읽도록 한다.
        dispose_engine()
        raise RuntimeError(
            f"{_BACKEND_ENV}=cubrid but the CUBRID server at {_CUBRID_URL_ENV} is not "
            f"reachable; refusing to start. Underlying error: {exc}"
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
