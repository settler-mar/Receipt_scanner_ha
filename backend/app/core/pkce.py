"""Shared PKCE (RFC 7636) helper — code_verifier/code_challenge pair generation,
used by any adapter doing an OAuth authorization_code flow (atb.py has its own
copy predating this; silpo.py uses this one)."""
from __future__ import annotations

import base64
import hashlib
import secrets


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge
