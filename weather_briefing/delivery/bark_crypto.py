"""Bark-compatible authenticated push encryption."""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_BARK_GCM_IV_CHARACTERS = 12
_BARK_AES_KEY_LENGTHS = frozenset({16, 24, 32})
_BARK_GCM_IV_RANDOM_BYTES = 9


@dataclass(frozen=True, slots=True)
class EncryptedBarkPayload:
    """Carry the Bark ciphertext and per-message GCM IV."""

    ciphertext: str
    iv: str


class BarkEncryptor:
    """Encrypt Bark request parameters with AES-GCM."""

    def __init__(self, key: str, iv: str) -> None:
        """Validate the Bark app encryption settings."""
        try:
            key_bytes = key.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("Bark encryption key must contain only ASCII characters") from exc
        if len(key_bytes) not in _BARK_AES_KEY_LENGTHS:
            raise ValueError("Bark encryption key must contain 16, 24, or 32 ASCII characters")
        try:
            iv_bytes = iv.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("Bark GCM IV must contain only ASCII characters") from exc
        if len(iv_bytes) != _BARK_GCM_IV_CHARACTERS:
            raise ValueError("Bark GCM IV must contain exactly 12 ASCII characters")
        self._cipher = AESGCM(key_bytes)

    def encrypt(self, parameters: Mapping[str, object]) -> EncryptedBarkPayload:
        """Serialize and encrypt one Bark parameter object."""
        iv = secrets.token_urlsafe(_BARK_GCM_IV_RANDOM_BYTES)
        plaintext = json.dumps(parameters, ensure_ascii=False, separators=(",", ":")).encode()
        ciphertext_and_tag = self._cipher.encrypt(iv.encode("ascii"), plaintext, None)
        return EncryptedBarkPayload(
            ciphertext=base64.b64encode(ciphertext_and_tag).decode("ascii"),
            iv=iv,
        )
