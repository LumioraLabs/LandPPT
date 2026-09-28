"""Envelope-style encryption for secrets persisted in application tables."""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from .config import app_config


ENCRYPTED_VALUE_PREFIX = "enc:v1:"


class SecretDecryptionError(ValueError):
    """Stored ciphertext could not be decrypted with the configured keys."""


def _derive_fernet_key(value: str) -> bytes:
    """Accept a Fernet key or derive one from an application secret."""
    candidate = str(value or "").strip().encode("utf-8")
    try:
        Fernet(candidate)
        return candidate
    except (TypeError, ValueError):
        return base64.urlsafe_b64encode(hashlib.sha256(candidate).digest())


@lru_cache(maxsize=8)
def _cipher(configured_keys: str, fallback_secret: str) -> MultiFernet:
    raw_keys = [key.strip() for key in configured_keys.split(",") if key.strip()]
    if not raw_keys:
        raw_keys = [fallback_secret]
    return MultiFernet([Fernet(_derive_fernet_key(key)) for key in raw_keys])


def _get_cipher() -> MultiFernet:
    return _cipher(
        str(app_config.config_encryption_keys or ""),
        str(app_config.secret_key or ""),
    )


def encrypt_secret(value: object) -> str:
    """Encrypt a plaintext value; already encrypted values remain unchanged."""
    plaintext = str(value or "")
    if not plaintext or plaintext.startswith(ENCRYPTED_VALUE_PREFIX):
        return plaintext
    token = _get_cipher().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{ENCRYPTED_VALUE_PREFIX}{token}"


def decrypt_secret(value: object) -> str:
    """Decrypt ciphertext while accepting legacy plaintext for migration."""
    stored = str(value or "")
    if not stored.startswith(ENCRYPTED_VALUE_PREFIX):
        return stored
    token = stored[len(ENCRYPTED_VALUE_PREFIX) :]
    try:
        return _get_cipher().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise SecretDecryptionError(
            "Stored secret cannot be decrypted; check LANDPPT_CONFIG_ENCRYPTION_KEYS"
        ) from exc


def prepare_config_value(value: object, config_type: str) -> str:
    """Encrypt database configuration values declared as passwords."""
    serialized = str(value or "")
    return encrypt_secret(serialized) if config_type == "password" else serialized


def reveal_config_value(value: object, config_type: str) -> str:
    """Decrypt database configuration values declared as passwords."""
    return decrypt_secret(value) if config_type == "password" else str(value or "")
