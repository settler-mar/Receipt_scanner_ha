"""Encrypted storage for the ДПС open-register API token."""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.security import decrypt, encrypt
from app.models.models import AppSetting

logger = logging.getLogger(__name__)

TOKEN_KEY = "tax_cabinet_token"


def get_token(db: Session) -> str | None:
    row = db.get(AppSetting, TOKEN_KEY)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    encrypted = value.get("enc")
    if not encrypted:
        return None
    try:
        return decrypt(encrypted) or None
    except Exception:  # noqa: BLE001 — corrupt/old secret must not break scanning
        logger.warning("stored tax cabinet token could not be decrypted", exc_info=True)
        return None


def token_hint(db: Session) -> str | None:
    token = get_token(db)
    if not token:
        return None
    return f"••••{token[-4:]}" if len(token) > 4 else "••••"


def set_token(db: Session, token: str | None) -> None:
    clean = (token or "").strip()
    row = db.get(AppSetting, TOKEN_KEY)
    if not clean:
        if row is not None:
            db.delete(row)
    else:
        payload = {"enc": encrypt(clean)}
        if row is None:
            db.add(AppSetting(key=TOKEN_KEY, value=payload))
        else:
            row.value = payload
    db.commit()
