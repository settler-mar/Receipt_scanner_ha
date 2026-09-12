"""Shared helper to persist an adapter's AuthResult onto an Account's Credential —
used by the phone/OTP login endpoints and the OAuth-browser callback alike."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.adapters.base import AuthResult
from app.core.security import encrypt
from app.models.models import Account, Credential


def upsert_credential(db: Session, account: Account, result: AuthResult) -> None:
    # Bind a newly-created credential through the relationship, not only by its
    # foreign key.  Login endpoints may start collection immediately in the same
    # request; without this assignment ``account.credential`` remains the cached
    # pre-login ``None`` until a fresh DB session is opened.
    cred = account.credential
    if cred is None:
        cred = Credential(account=account)
        db.add(cred)
    if result.access_token is not None:
        cred.access_token_enc = encrypt(result.access_token)
    if result.refresh_token is not None:
        cred.refresh_token_enc = encrypt(result.refresh_token)
    if result.expires_at is not None:
        cred.expires_at = result.expires_at
    cred.extra = {**(cred.extra or {}), **result.extra}
