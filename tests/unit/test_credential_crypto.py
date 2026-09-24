"""``AesGcmCredentialCipher`` 의 거부 경로 회귀 테스트 (#593).

이 모듈은 provider credential 을 감싸는 암호 경계다. 정상 왕복만 테스트돼 있으면
거부 로직이 깨져도(잘못된 키를 받아들이거나 변조를 눈감아도) 스위트가 초록으로 남는다.
여기서는 "무엇을 거부해야 하는가"만 검증한다.
"""

from __future__ import annotations

import base64
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from kpubdata_builder.credentials import AesGcmCredentialCipher
from kpubdata_builder.credentials.crypto import CredentialCryptoError

_KEY = b"k" * 32
_AAD = b"owner-1:datago"


def _cipher() -> AesGcmCredentialCipher:
    return AesGcmCredentialCipher(_KEY)


# --- master key 검증 -------------------------------------------------------


@pytest.mark.parametrize("size", [0, 16, 24, 31, 33, 64])
def test_master_key_must_be_exactly_32_bytes(size: int) -> None:
    """AES-256 만 허용한다 — 16/24 바이트(AES-128/192)도 거부한다."""
    with pytest.raises(CredentialCryptoError, match="exactly 32 bytes"):
        AesGcmCredentialCipher(b"k" * size)


def test_from_base64_accepts_urlsafe_key() -> None:
    encoded = base64.urlsafe_b64encode(_KEY).decode("ascii")
    cipher = AesGcmCredentialCipher.from_base64(encoded)
    assert cipher.decrypt(cipher.encrypt("secret", associated_data=_AAD), associated_data=_AAD) == (
        "secret"
    )


def test_from_base64_rejects_malformed_base64() -> None:
    """패딩이 깨진 base64 는 binascii.Error → CredentialCryptoError 로 변환된다."""
    with pytest.raises(CredentialCryptoError, match="URL-safe base64"):
        AesGcmCredentialCipher.from_base64("AAAAA")


def test_from_base64_rejects_non_ascii_key() -> None:
    """ascii 밖 문자는 encode 단계에서 걸린다 — UnicodeEncodeError 도 같은 에러로 묶는다."""
    with pytest.raises(CredentialCryptoError, match="URL-safe base64"):
        AesGcmCredentialCipher.from_base64("마스터키")


def test_from_base64_propagates_length_check() -> None:
    """형식은 맞지만 32바이트로 풀리지 않는 키는 길이 검증에서 거부된다."""
    encoded = base64.urlsafe_b64encode(b"short").decode("ascii")
    with pytest.raises(CredentialCryptoError, match="exactly 32 bytes"):
        AesGcmCredentialCipher.from_base64(encoded)


# --- encrypt ---------------------------------------------------------------


def test_encrypt_rejects_empty_plaintext() -> None:
    """빈 credential 을 저장하면 '설정됨'과 '비어 있음'을 구분할 수 없게 된다."""
    with pytest.raises(CredentialCryptoError, match="must not be empty"):
        _cipher().encrypt("", associated_data=_AAD)


def test_encrypt_uses_a_fresh_nonce_each_call() -> None:
    """같은 평문이라도 ciphertext 가 같으면 안 된다(nonce 재사용 = AES-GCM 치명적)."""
    cipher = _cipher()
    first = cipher.encrypt("secret", associated_data=_AAD)
    second = cipher.encrypt("secret", associated_data=_AAD)
    assert first[:12] != second[:12]
    assert first != second


# --- decrypt ---------------------------------------------------------------


@pytest.mark.parametrize("size", [0, 1, 11, 12])
def test_decrypt_rejects_ciphertext_shorter_than_nonce(size: int) -> None:
    """nonce(12B) + tag 조차 없는 값은 슬라이싱 전에 거부한다."""
    with pytest.raises(CredentialCryptoError, match="stored credential is invalid"):
        _cipher().decrypt(b"\x00" * size, associated_data=_AAD)


def test_decrypt_rejects_tampered_ciphertext() -> None:
    """본문 한 바이트만 뒤집어도 GCM tag 검증에서 InvalidTag 가 난다."""
    cipher = _cipher()
    blob = bytearray(cipher.encrypt("secret", associated_data=_AAD))
    blob[-1] ^= 0x01
    with pytest.raises(CredentialCryptoError, match="integrity validation"):
        cipher.decrypt(bytes(blob), associated_data=_AAD)


def test_decrypt_rejects_tampered_nonce() -> None:
    cipher = _cipher()
    blob = bytearray(cipher.encrypt("secret", associated_data=_AAD))
    blob[0] ^= 0x01
    with pytest.raises(CredentialCryptoError, match="integrity validation"):
        cipher.decrypt(bytes(blob), associated_data=_AAD)


def test_decrypt_rejects_mismatched_associated_data() -> None:
    """AAD 는 credential 을 owner/provider 에 묶는다 — 다른 소유자로는 풀 수 없어야 한다."""
    cipher = _cipher()
    blob = cipher.encrypt("secret", associated_data=b"owner-1:datago")
    with pytest.raises(CredentialCryptoError, match="integrity validation"):
        cipher.decrypt(blob, associated_data=b"owner-2:datago")


def test_decrypt_rejects_ciphertext_from_another_key() -> None:
    """마스터 키를 교체하면 기존 credential 은 복호화되지 않는다(README 의 rotation 경고)."""
    blob = AesGcmCredentialCipher(b"a" * 32).encrypt("secret", associated_data=_AAD)
    with pytest.raises(CredentialCryptoError, match="integrity validation"):
        AesGcmCredentialCipher(b"b" * 32).decrypt(blob, associated_data=_AAD)


def test_decrypt_rejects_non_utf8_plaintext() -> None:
    """tag 는 맞지만 본문이 utf-8 이 아닌 경우(외부에서 주입된 행)도 같은 에러로 묶는다."""
    nonce = os.urandom(12)
    blob = nonce + AESGCM(_KEY).encrypt(nonce, b"\xff\xfe", _AAD)
    with pytest.raises(CredentialCryptoError, match="integrity validation"):
        _cipher().decrypt(blob, associated_data=_AAD)


def test_roundtrip_preserves_unicode_credential() -> None:
    cipher = _cipher()
    secret = "서비스키-Ω-🔑"
    assert cipher.decrypt(cipher.encrypt(secret, associated_data=_AAD), associated_data=_AAD) == (
        secret
    )
