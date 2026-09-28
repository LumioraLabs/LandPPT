"""Password hashing and migration helpers.

LandPPT historically stored bare SHA-256 password digests.  They remain
verifiable only so an existing user can be transparently migrated after a
successful login.  New hashes use Argon2id with the minimum memory-hard profile
recommended by OWASP for interactive logins.
"""

from __future__ import annotations

import hashlib
import hmac
import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type


_LEGACY_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


def hash_password(password: str) -> str:
    """Return an adaptive, salted password hash."""
    if not isinstance(password, str) or not password:
        raise ValueError("Password must not be empty")
    return _PASSWORD_HASHER.hash(password)


def is_legacy_password_hash(stored_hash: str) -> bool:
    """Identify the legacy unsalted SHA-256 representation."""
    return bool(_LEGACY_SHA256_RE.fullmatch(str(stored_hash or "")))


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify either a current hash or a legacy hash in constant time."""
    if not isinstance(password, str) or not stored_hash:
        return False

    if is_legacy_password_hash(stored_hash):
        candidate = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(candidate, stored_hash.lower())

    try:
        return bool(_PASSWORD_HASHER.verify(stored_hash, password))
    except (InvalidHashError, VerificationError, VerifyMismatchError, ValueError, TypeError):
        return False


def password_hash_needs_update(stored_hash: str) -> bool:
    """Return whether a verified password should be re-hashed."""
    if is_legacy_password_hash(stored_hash):
        return True
    try:
        return bool(_PASSWORD_HASHER.check_needs_rehash(stored_hash))
    except (InvalidHashError, ValueError, TypeError):
        return True
