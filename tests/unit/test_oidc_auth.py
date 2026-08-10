"""OIDC Bearer 인증 테스트 (#385, B3).

로컬 RSA 키쌍으로 서명한 ID token을 검증한다 — 외부 IdP에 의존하지 않는다.
JWKS 조회는 _get_jwks_client를 mock해 네트워크 없이 검증 로직만 테스트한다.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kpubdata_builder.service.auth import (
    AuthError,
    Principal,
    authenticate,
    validate_oidc_config,
)

_ISSUER = "https://accounts.google.com"
_AUDIENCE = "builder-test-client"


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """각 테스트 전에 인증 관련 환경변수를 초기화한다 — dev-mode 등 누출 차단."""
    for key in (
        "KPUBDATA_BUILDER_DEV_MODE",
        "KPUBDATA_BUILDER_API_KEY",
        "OIDC_ISSUER",
        "OIDC_AUDIENCE",
        "OIDC_JWKS_URL",
        "OIDC_JWKS_TTL",
    ):
        monkeypatch.delenv(key, raising=False)


class _FakeSigningKey:
    """PyJWKClient.get_signing_key_from_jwt의 반환을 흉내낸다."""

    def __init__(self, key: object) -> None:
        self.key = key


class _FakeJWKSClient:
    """JWKS 조회 mock — 네트워크 없이 public key를 반환한다."""

    def __init__(self, public_key: object) -> None:
        self._pubkey = public_key

    def get_signing_key_from_jwt(self, token: str) -> _FakeSigningKey:
        return _FakeSigningKey(self._pubkey)


class _FailingJWKSClient:
    """JWKS 조회 실패를 시뮬레이션 (503 검증용)."""

    def get_signing_key_from_jwt(self, token: str) -> _FakeSigningKey:
        raise ConnectionError("jwks endpoint unreachable")


@pytest.fixture()
def rsa_keypair() -> tuple[bytes, object]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_pem, private_key.public_key()


@pytest.fixture()
def oidc_env(monkeypatch: pytest.MonkeyPatch, rsa_keypair: tuple[bytes, object]) -> bytes:
    """OIDC 활성 + JWKS mock. 토큰 서명용 private PEM을 반환한다."""
    private_pem, public_key = rsa_keypair
    monkeypatch.setenv("OIDC_ISSUER", _ISSUER)
    monkeypatch.setenv("OIDC_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("OIDC_JWKS_URL", "http://localhost:0/jwks.json")
    import kpubdata_builder.service.auth as auth_module

    monkeypatch.setattr(auth_module, "_get_jwks_client", lambda: _FakeJWKSClient(public_key))
    return private_pem


def _make_token(private_pem: bytes, **overrides: object) -> str:
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "sub": "user-1234567890",
        "email": "user@example.com",
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
    }
    payload.update(overrides)
    return jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": "test-key"})


class TestValidBearerToken:
    def test_valid_token_returns_oidc_principal(self, oidc_env: bytes) -> None:
        token = _make_token(oidc_env)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, Principal)
        assert result.kind == "oidc"
        # 식별자는 sub 앞 8자
        assert result.identifier == "user-123"

    def test_case_insensitive_bearer_prefix(self, oidc_env: bytes) -> None:
        token = _make_token(oidc_env)
        result = authenticate(bearer_token=f"bearer {token}")
        assert isinstance(result, Principal)
        assert result.kind == "oidc"


class TestInvalidTokens:
    def test_invalid_signature(self, oidc_env: bytes) -> None:
        # 다른 키로 서명 → fixture의 public key로 검증 실패
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_pem = other.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        token = _make_token(other_pem)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, AuthError)
        assert result.status_code == 401

    def test_expired_token(self, oidc_env: bytes) -> None:
        now = int(time.time())
        token = _make_token(oidc_env, exp=now - 120)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, AuthError)

    def test_wrong_audience(self, oidc_env: bytes) -> None:
        token = _make_token(oidc_env, aud="wrong-client")
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, AuthError)

    def test_wrong_issuer(self, oidc_env: bytes) -> None:
        token = _make_token(oidc_env, iss="https://evil.example.com")
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, AuthError)

    def test_email_not_verified(self, oidc_env: bytes) -> None:
        token = _make_token(oidc_env, email_verified=False)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, AuthError)
        assert "email" in result.reason

    def test_malformed_authorization_header(self, oidc_env: bytes) -> None:
        result = authenticate(bearer_token="not-a-bearer-scheme")
        assert isinstance(result, AuthError)


class TestJWKSFailure:
    def test_jwks_unavailable_returns_503(
        self, monkeypatch: pytest.MonkeyPatch, oidc_env: bytes
    ) -> None:
        token = _make_token(oidc_env)
        import kpubdata_builder.service.auth as auth_module

        monkeypatch.setattr(auth_module, "_get_jwks_client", lambda: _FailingJWKSClient())
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, AuthError)
        assert result.status_code == 503


class TestFallbackToApiKey:
    def test_oidc_enabled_falls_back_to_api_key_when_no_bearer(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
        result = authenticate(api_key="secret")
        assert isinstance(result, Principal)
        assert result.kind == "service"

    def test_oidc_disabled_ignores_bearer(
        self, monkeypatch: pytest.MonkeyPatch, rsa_keypair: tuple[bytes, object]
    ) -> None:
        # OIDC_ISSUER 미설정 → Bearer 무시, API key 경로로
        monkeypatch.delenv("OIDC_ISSUER", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
        result = authenticate(api_key="secret", bearer_token="Bearer some.jwt.token")
        assert isinstance(result, Principal)
        assert result.kind == "service"

    def test_dev_mode_short_circuits_bearer(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
        result = authenticate(bearer_token="Bearer anything")
        assert isinstance(result, Principal)
        assert result.kind == "dev"


class TestValidateOidcConfig:
    def test_no_op_when_oidc_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OIDC_ISSUER", raising=False)
        validate_oidc_config()  # 예외 없음

    def test_rejects_when_audience_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OIDC_ISSUER", _ISSUER)
        monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
        with pytest.raises(RuntimeError, match="OIDC_AUDIENCE"):
            validate_oidc_config()

    def test_rejects_when_no_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OIDC_ISSUER", _ISSUER)
        monkeypatch.setenv("OIDC_AUDIENCE", _AUDIENCE)
        with pytest.raises(RuntimeError, match="allowlist"):
            validate_oidc_config()


class TestAllowlistGate:
    """허용 목록 게이트 (#386). Google은 공개 IdP — 허용 목록 없이는 인터넷 전체에 노출."""

    def test_hd_allowlist_match(self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OIDC_ALLOWED_HD", "example.com")
        token = _make_token(oidc_env, hd="example.com")
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, Principal)
        assert result.kind == "oidc"

    def test_hd_allowlist_miss(self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OIDC_ALLOWED_HD", "other.com")
        token = _make_token(oidc_env, hd="example.com")
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, AuthError)
        assert result.status_code == 403

    def test_subject_allowlist_match(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OIDC_ALLOWED_SUBJECTS", "user-1234567890")
        token = _make_token(oidc_env)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, Principal)

    def test_email_allowlist_match(self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OIDC_ALLOWED_EMAILS", "user@example.com")
        token = _make_token(oidc_env)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, Principal)

    def test_hd_missing_rejected_when_hd_required(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OIDC_ALLOWED_HD", "example.com")
        token = _make_token(oidc_env)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, AuthError)
        assert result.status_code == 403

    def test_multiple_lists_match_any(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OIDC_ALLOWED_HD", "other.com")
        monkeypatch.setenv("OIDC_ALLOWED_EMAILS", "user@example.com")
        token = _make_token(oidc_env, hd="example.com")
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, Principal)


class _FakeDiscoveryResp:
    """urllib urlopen 반환 흉내 (context manager + read)."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeDiscoveryResp:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class TestJwksDiscovery:
    """OIDC discovery (RFC 8414) 기반 JWKS URL 해석 (#435).

    기존 ``issuer + /.well-known/jwks.json`` 추정이 Google에서 404 → 503 실패를
    일으킨 문제 수정. discovery 문서의 jwks_uri를 읽고 TTL 캐시한다.
    """

    def _clear_cache(self) -> None:
        import kpubdata_builder.service.auth as auth_module

        auth_module._discovery_cache.clear()

    def test_discover_reads_jwks_uri(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """discovery 문서의 jwks_uri 필드를 반환한다 (Google 경로)."""
        import urllib.request

        import kpubdata_builder.service.auth as auth_module

        self._clear_cache()
        captured: list[str] = []
        doc = b'{"issuer":"https://accounts.google.com","jwks_uri":"https://www.googleapis.com/oauth2/v3/certs"}'

        def _fake_urlopen(url: str, timeout: float) -> _FakeDiscoveryResp:
            captured.append(url)
            return _FakeDiscoveryResp(doc)

        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

        result = auth_module._discover_jwks_uri("https://accounts.google.com")
        assert result == "https://www.googleapis.com/oauth2/v3/certs"
        assert "accounts.google.com/.well-known/openid-configuration" in captured[0]

    def test_oidc_jwks_url_explicit_bypasses_discovery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OIDC_JWKS_URL 명시 시 discovery를 건너뛴다 (override)."""
        import kpubdata_builder.service.auth as auth_module

        monkeypatch.setenv("OIDC_JWKS_URL", "http://explicit/jwks.json")
        assert auth_module._oidc_jwks_url() == "http://explicit/jwks.json"

    def test_discover_caches_within_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """동일 issuer는 TTL 내 재조회 시 urlopen을 한 번만 부른다."""
        import urllib.request

        import kpubdata_builder.service.auth as auth_module

        self._clear_cache()
        call_count = [0]

        def _fake_urlopen(url: str, timeout: float) -> _FakeDiscoveryResp:
            call_count[0] += 1
            return _FakeDiscoveryResp(b'{"jwks_uri":"https://cached/certs"}')

        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

        first = auth_module._discover_jwks_uri("https://idp.example.com")
        second = auth_module._discover_jwks_uri("https://idp.example.com")
        assert first == second == "https://cached/certs"
        assert call_count[0] == 1, "캐시 hit면 urlopen을 다시 부르지 않는다"

    def test_discover_raises_when_jwks_uri_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """discovery 문서에 jwks_uri 필드가 없으면 RuntimeError."""
        import urllib.request

        import kpubdata_builder.service.auth as auth_module

        self._clear_cache()
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda url, timeout: _FakeDiscoveryResp(b'{"issuer":"https://idp.example.com"}'),
        )

        with pytest.raises(RuntimeError, match="jwks_uri"):
            auth_module._discover_jwks_uri("https://idp.example.com")

    def test_oidc_jwks_url_raises_when_no_issuer_no_explicit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OIDC_ISSUER 미설정 + OIDC_JWKS_URL 미설정 → RuntimeError (방어)."""
        import kpubdata_builder.service.auth as auth_module

        monkeypatch.delenv("OIDC_ISSUER", raising=False)
        monkeypatch.delenv("OIDC_JWKS_URL", raising=False)
        with pytest.raises(RuntimeError, match="OIDC_ISSUER"):
            auth_module._oidc_jwks_url()
