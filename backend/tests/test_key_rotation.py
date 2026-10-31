from cryptography.fernet import Fernet
from sqlalchemy import text

from app.core import crypto
from app.models import IntegrationCredential


def test_versioned_keys_and_rotation(db, settings):
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    settings(token_encryption_keys=old, token_encryption_key="")
    db.add(IntegrationCredential(org_id=1, provider="google", encrypted_refresh_token=crypto.encrypt("1//secret"), scopes=[]))
    db.commit()
    before = db.execute(text("SELECT encrypted_refresh_token FROM integration_credentials")).scalar_one()
    settings(token_encryption_keys=f"{new},{old}")  # new primary, old still readable
    assert crypto.decrypt(before) == "1//secret"
    assert crypto.rotate_all(db)["integration_credentials"] == 1
    after = db.execute(text("SELECT encrypted_refresh_token FROM integration_credentials")).scalar_one()
    settings(token_encryption_keys=new)  # old key removed
    assert crypto.decrypt(after) == "1//secret"
    import pytest
    with pytest.raises(ValueError):
        crypto.decrypt(before)  # pre-rotation ciphertext no longer readable without the old key
