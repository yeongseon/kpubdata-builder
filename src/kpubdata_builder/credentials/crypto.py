"""Credential 암호화 경계.

저장소에는 nonce와 AES-GCM ciphertext/tag만 기록한다. master key는 저장소와
분리된 환경설정에서 주입하며 이 모듈이나 DB에 기록하지 않는다.
"""

from __future__ import annotations

import base64
import binascii
import os
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_BYTES = 12


class CredentialCryptoError(ValueError):
    """credential 암복호화 설정 또는 무결성 오류."""


class CredentialCipher(Protocol):
    """저장소가 사용하는 인증 암호 인터페이스."""

    def encrypt(self, plaintext: str, *, associated_data: bytes) -> bytes: ...

    def decrypt(self, ciphertext: bytes, *, associated_data: bytes) -> str: ...


class AesGcmCredentialCipher:
    """256-bit AES-GCM credential 암호기."""

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != 32:
            raise CredentialCryptoError("credential master key must decode to exactly 32 bytes")
        self._cipher = AESGCM(master_key)

    @classmethod
    def from_base64(cls, encoded_key: str) -> AesGcmCredentialCipher:
        """URL-safe base64 master key로 암호기를 생성한다."""
        try:
            key = base64.urlsafe_b64decode(encoded_key.encode("ascii"))
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise CredentialCryptoError("credential master key must be URL-safe base64") from exc
        return cls(key)

    def encrypt(self, plaintext: str, *, associated_data: bytes) -> bytes:
        if not plaintext:
            raise CredentialCryptoError("credential must not be empty")
        nonce = os.urandom(_NONCE_BYTES)
        return nonce + self._cipher.encrypt(nonce, plaintext.encode("utf-8"), associated_data)

    def decrypt(self, ciphertext: bytes, *, associated_data: bytes) -> str:
        if len(ciphertext) <= _NONCE_BYTES:
            raise CredentialCryptoError("stored credential is invalid")
        nonce, encrypted = ciphertext[:_NONCE_BYTES], ciphertext[_NONCE_BYTES:]
        try:
            plaintext = self._cipher.decrypt(nonce, encrypted, associated_data)
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as exc:
            raise CredentialCryptoError("stored credential failed integrity validation") from exc


__all__ = ["AesGcmCredentialCipher", "CredentialCipher", "CredentialCryptoError"]
