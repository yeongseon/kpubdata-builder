"""인증 실패 스로틀 테스트.

슬라이딩 윈도 카운터 자체(AuthFailureThrottle)와, dispatch 인증 게이트에 붙었을 때의
동작(401 누적 → 429, 성공 시 초기화)을 함께 확인한다.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterable
from http.server import HTTPServer
from pathlib import Path

import pytest

from kpubdata_builder.service.app import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth_throttle import AuthFailureThrottle
from kpubdata_builder.service.http import make_handler

from .test_service import _FakeClient

_CLIENT = "203.0.113.7"


class _FakeClock:
    """테스트가 시간을 직접 진행시킨다 — 실제 대기 없이 윈도 만료를 검증한다."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _throttle(clock: _FakeClock, *, limit: int = 3, window: float = 60.0) -> AuthFailureThrottle:
    return AuthFailureThrottle(limit=limit, window_seconds=window, time_source=clock)


class TestAuthFailureThrottle:
    def test_allows_failures_below_the_limit(self) -> None:
        clock = _FakeClock()
        throttle = _throttle(clock)
        for _ in range(2):
            throttle.record_failure(_CLIENT)
        assert throttle.retry_after(_CLIENT) is None

    def test_blocks_once_the_limit_is_reached(self) -> None:
        clock = _FakeClock()
        throttle = _throttle(clock)
        for _ in range(3):
            throttle.record_failure(_CLIENT)
        assert throttle.retry_after(_CLIENT) == 60

    def test_retry_after_shrinks_as_the_window_slides(self) -> None:
        clock = _FakeClock()
        throttle = _throttle(clock)
        for _ in range(3):
            throttle.record_failure(_CLIENT)
        clock.advance(45)
        assert throttle.retry_after(_CLIENT) == 15

    def test_releases_the_client_after_the_window_expires(self) -> None:
        clock = _FakeClock()
        throttle = _throttle(clock)
        for _ in range(3):
            throttle.record_failure(_CLIENT)
        clock.advance(61)
        assert throttle.retry_after(_CLIENT) is None

    def test_successful_authentication_clears_the_failures(self) -> None:
        clock = _FakeClock()
        throttle = _throttle(clock)
        for _ in range(3):
            throttle.record_failure(_CLIENT)
        throttle.record_success(_CLIENT)
        assert throttle.retry_after(_CLIENT) is None

    def test_failures_are_counted_per_client(self) -> None:
        clock = _FakeClock()
        throttle = _throttle(clock)
        for _ in range(3):
            throttle.record_failure(_CLIENT)
        assert throttle.retry_after("198.51.100.4") is None

    def test_unidentifiable_client_is_never_throttled(self) -> None:
        clock = _FakeClock()
        throttle = _throttle(clock)
        for _ in range(5):
            throttle.record_failure(None)
        assert throttle.retry_after(None) is None

    def test_disabled_when_limit_is_not_positive(self) -> None:
        clock = _FakeClock()
        throttle = AuthFailureThrottle(limit=0, window_seconds=60.0, time_source=clock)
        assert not throttle.enabled
        for _ in range(10):
            throttle.record_failure(_CLIENT)
        assert throttle.retry_after(_CLIENT) is None

    def test_tracked_clients_stay_bounded(self) -> None:
        # 서로 다른 IP로 실패를 뿌려도 추적 dict가 무한히 자라지 않아야 한다.
        clock = _FakeClock()
        throttle = AuthFailureThrottle(
            limit=3, window_seconds=60.0, max_clients=8, time_source=clock
        )
        for index in range(200):
            throttle.record_failure(f"198.51.100.{index}")
        assert len(throttle._failures) <= 8

    def test_reads_limit_and_window_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT", "2")
        monkeypatch.setenv("KPUBDATA_BUILDER_AUTH_FAILURE_WINDOW_SECONDS", "30")
        clock = _FakeClock()
        throttle = AuthFailureThrottle(time_source=clock)
        throttle.record_failure(_CLIENT)
        assert throttle.retry_after(_CLIENT) is None
        throttle.record_failure(_CLIENT)
        assert throttle.retry_after(_CLIENT) == 30

    def test_malformed_env_falls_back_to_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 설정 오타가 서비스 기동을 깨뜨리지 않아야 한다.
        monkeypatch.setenv("KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT", "many")
        monkeypatch.setenv("KPUBDATA_BUILDER_AUTH_FAILURE_WINDOW_SECONDS", "-5")
        throttle = AuthFailureThrottle(time_source=_FakeClock())
        assert throttle.enabled


class TestDispatchAuthThrottle:
    """인증 게이트에 붙은 스로틀 — 401 반복이 429로 바뀌는지 확인한다."""

    @pytest.fixture
    def service(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BuilderService:
        # conftest의 dev-mode를 끄고 실제 인증 게이트를 태운다.
        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "correct-key")
        client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda **_kwargs: client)
        service._auth_throttle = AuthFailureThrottle(limit=3, window_seconds=60.0)
        return service

    def _version(self, service: BuilderService, *, api_key: str, client_id: str | None) -> int:
        response = dispatch(service, "GET", "/version", None, api_key=api_key, client_id=client_id)
        assert isinstance(response, ServiceResponse)
        if response.status_code == 429:
            assert response.body["code"] == "auth_throttled"
            assert isinstance(response.body["retry_after_seconds"], int)
        return response.status_code

    def test_repeated_bad_credentials_become_429(self, service: BuilderService) -> None:
        for _ in range(3):
            assert self._version(service, api_key="wrong", client_id=_CLIENT) == 401
        assert self._version(service, api_key="wrong", client_id=_CLIENT) == 429
        # 올바른 키를 내밀어도 스로틀 구간에서는 막힌다 — 검증 비용 자체를 태우지 않는다.
        assert self._version(service, api_key="correct-key", client_id=_CLIENT) == 429

    def test_other_clients_are_unaffected(self, service: BuilderService) -> None:
        for _ in range(4):
            self._version(service, api_key="wrong", client_id=_CLIENT)
        assert self._version(service, api_key="correct-key", client_id="198.51.100.9") == 200

    def test_success_resets_the_counter(self, service: BuilderService) -> None:
        for _ in range(2):
            assert self._version(service, api_key="wrong", client_id=_CLIENT) == 401
        assert self._version(service, api_key="correct-key", client_id=_CLIENT) == 200
        # 카운터가 비었으므로 다시 한도만큼의 실패 여유가 있다.
        for _ in range(3):
            assert self._version(service, api_key="wrong", client_id=_CLIENT) == 401
        assert self._version(service, api_key="wrong", client_id=_CLIENT) == 429

    def test_healthz_stays_reachable_while_throttled(self, service: BuilderService) -> None:
        for _ in range(4):
            self._version(service, api_key="wrong", client_id=_CLIENT)
        response = dispatch(service, "GET", "/healthz", None, client_id=_CLIENT)
        assert isinstance(response, ServiceResponse)
        assert response.status_code == 200


class TestHttpAuthThrottle:
    """HTTP 계층이 peer 주소를 스로틀 식별자로 넘기는지 — 실제 소켓으로 확인한다."""

    @pytest.fixture
    def server(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterable[str]:
        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
        # 서비스 생성 전에 한도를 낮춰 두 번의 실패만으로 스로틀을 태운다.
        monkeypatch.setenv("KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT", "2")
        client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda **_kwargs: client)
        httpd = HTTPServer(("127.0.0.1", 0), make_handler(service))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://{httpd.server_address[0]}:{httpd.server_address[1]}"
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=1.0)

    def test_repeated_unauthenticated_requests_get_429(self, server: str) -> None:
        def _status(api_key: str) -> tuple[int, dict[str, object]]:
            request = urllib.request.Request(f"{server}/version", headers={"X-API-Key": api_key})
            try:
                with urllib.request.urlopen(request, timeout=2.0) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        assert _status("wrong")[0] == 401
        assert _status("wrong")[0] == 401
        status, body = _status("wrong")
        assert status == 429
        assert body["code"] == "auth_throttled"
        assert isinstance(body["retry_after_seconds"], int)
