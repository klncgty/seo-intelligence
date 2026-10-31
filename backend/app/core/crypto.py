"""Encryption at rest for integration secrets — versioned keys (MultiFernet).

TOKEN_ENCRYPTION_KEYS="<new>,<old>,…" (first = primary for new writes; all are tried for reads). The legacy single
TOKEN_ENCRYPTION_KEY still works. Rotation: put the new key first, run `uv run seo rotate-keys` (re-encrypts every stored
secret with the primary key), then remove the old key. See docs/runbooks/README.md.
"""

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import get_settings


class EncryptionNotConfigured(RuntimeError):
    pass


def _keys() -> list[str]:
    s = get_settings()
    keys = [k.strip() for k in (s.token_encryption_keys or "").split(",") if k.strip()]
    if not keys and s.token_encryption_key:
        keys = [s.token_encryption_key]
    return keys


def _fernet() -> MultiFernet:
    keys = _keys()
    if not keys:
        raise EncryptionNotConfigured("TOKEN_ENCRYPTION_KEY(S) is not set")
    return MultiFernet([Fernet(k.encode()) for k in keys])


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Stored credential cannot be decrypted with the configured keys") from exc


def rotate(ciphertext: str) -> str:
    return _fernet().rotate(ciphertext.encode()).decode()


def encryption_configured() -> bool:
    return bool(_keys())


ENCRYPTED_COLUMNS = [  # (table, primary key column, encrypted column)
    ("integration_credentials", "id", "encrypted_refresh_token"),
    ("notification_channels", "id", "config_encrypted"),
    ("users", "id", "totp_secret_encrypted"),
    ("oauth_states", "state", "code_verifier_encrypted"),
]


def rotate_all(db) -> dict:
    """Re-encrypt every stored secret with the primary key (system context; bypasses RLS)."""
    from sqlalchemy import text
    out = {}
    for table, pk, col in ENCRYPTED_COLUMNS:
        rows = db.execute(text(f"SELECT {pk}, {col} FROM {table} WHERE {col} IS NOT NULL")).all()  # nosec B608 - fixed identifiers
        for key, value in rows:
            db.execute(text(f"UPDATE {table} SET {col} = :v WHERE {pk} = :k"), {"v": rotate(value), "k": key})  # nosec B608
        out[table] = len(rows)
    db.commit()
    return out
