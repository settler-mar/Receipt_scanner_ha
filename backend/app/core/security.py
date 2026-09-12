"""Symmetric encryption for tokens/secrets stored at rest (Fernet)."""
import base64
import hashlib

from cryptography.fernet import Fernet

from app.core.config import settings


def _fernet() -> Fernet:
    # Derive a valid 32-byte urlsafe-base64 key from whatever secret_key is configured,
    # so the setting doesn't have to be a perfectly formed Fernet key.
    digest = hashlib.sha256(settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str | None) -> str | None:
    if value is None:
        return None
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str | None) -> str | None:
    if token is None:
        return None
    return _fernet().decrypt(token.encode()).decode()
