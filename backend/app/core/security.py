"""Password hashing (Argon2id), opaque tokens, API keys and TOTP (RFC 6238)."""

import base64
import hashlib
import hmac
import secrets
import struct
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_ph = PasswordHasher()  # argon2id with library defaults (RFC 9106 recommended params)
API_KEY_PREFIX = "sgi"


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    return _ph.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    if not stored:
        _ph.hash("dummy-password-for-timing")  # keep timing similar for unknown users
        return False
    try:
        return _ph.verify(stored, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_token(nbytes: int = 32) -> tuple[str, str]:
    """Returns (plaintext, sha256 hash). Only the hash is stored."""
    t = secrets.token_urlsafe(nbytes)
    return t, sha256(t)


def new_api_key() -> tuple[str, str, str]:
    """Returns (full key shown once, prefix, hash). Format: sgi_<prefix>_<secret>."""
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    full = f"{API_KEY_PREFIX}_{prefix}_{secret}"
    return full, prefix, sha256(full)


def parse_api_key(full: str) -> str | None:
    parts = full.split("_", 2)
    if len(parts) != 3 or parts[0] != API_KEY_PREFIX or len(parts[1]) != 8:
        return None
    return parts[1]


# ---------------------------------------------------------------- TOTP

def totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_at(secret: str, ts: float | None = None, step: int = 30, digits: int = 6) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = int((ts if ts is not None else time.time()) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** digits
    return str(code).zfill(digits)


def verify_totp(secret: str, code: str, window: int = 1, ts: float | None = None) -> bool:
    now = ts if ts is not None else time.time()
    code = (code or "").strip()
    return any(hmac.compare_digest(totp_at(secret, now + i * 30), code) for i in range(-window, window + 1))


def totp_uri(secret: str, email: str, issuer: str = "Growth Intelligence") -> str:
    from urllib.parse import quote
    return f"otpauth://totp/{quote(issuer)}:{quote(email)}?secret={secret}&issuer={quote(issuer)}"
