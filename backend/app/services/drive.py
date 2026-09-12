"""Google Drive client for the personal-document inbox (ROADMAP.md Крок 3).

Auth model:
  * the OAuth *client* (client_id + client_secret) is an app registration. It can
    be entered on the Settings page (stored encrypted in `app_settings`, key
    `drive_oauth_client`) or supplied via `.env` / add-on options
    (`RS_GOOGLE_CLIENT_ID` / `RS_GOOGLE_CLIENT_SECRET`) — the stored value wins,
    the env value is the fallback. See DOCS.md for how to create it.
  * the *user* connects their Drive through the Settings page (a normal OAuth
    consent screen). The resulting refresh token is stored **encrypted** in
    `app_settings` (key `drive_oauth`, via app.core.security), same as the AI
    provider keys. Access tokens are short-lived and refreshed on demand.

This module never imports app.services.ai (the document branch must not — there
is a test). It only uses httpx, already a dependency.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decrypt, encrypt
from app.models.models import AppSetting

logger = logging.getLogger(__name__)

TOKEN_KEY = "drive_oauth"          # reserved app_settings key — encrypted refresh token
CLIENT_KEY = "drive_oauth_client"  # reserved app_settings key — client_id + encrypted secret

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 - URL, not a secret
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
API_BASE = "https://www.googleapis.com/drive/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"

FOLDER_MIME = "application/vnd.google-apps.folder"

# Google Drive only accepts a fixed set of folder colours (`folderColorRgb`);
# anything else snaps to the nearest one server-side. This is the legacy 24-colour
# palette — still accepted, and what `about.folderColorPalette` returns for most
# accounts. The live list is fetched via get_color_palette(); this is the fallback
# and also the set the UI renders. First entry doubles as "default / no colour".
DEFAULT_FOLDER_COLOR = "#c2c2c2"
FOLDER_COLORS = [
    "#c2c2c2", "#cabdbf", "#cca6ac", "#f691b2", "#cd74e6", "#a47ae2",
    "#ac725e", "#d06b64", "#f83a22", "#fa573c", "#ff7537", "#ffad46",
    "#fad165", "#fbe983", "#b3dc6c", "#7bd148", "#16a765", "#42d692",
    "#92e1c0", "#9fe1e7", "#9fc6e7", "#4986e7", "#9a9cff", "#b99aff",
]

# Full Drive access: needed to browse the user's existing folder tree and move
# uploaded files into a folder they pick. It is their own Drive on a LAN tool;
# the document branch is the only thing that ever touches it.
SCOPE = "https://www.googleapis.com/auth/drive"


class DriveError(RuntimeError):
    """Any Drive/OAuth failure; message is surfaced to the user."""


def redirect_uri() -> str:
    return f"{settings.base_url}/api/inbox/drive/callback"


# --------------------------------------------------------------------------- #
# OAuth client credentials — Settings-page value wins, .env is the fallback
# --------------------------------------------------------------------------- #
def _client_blob(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, CLIENT_KEY)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def get_client_id(db: Session) -> str:
    return (_client_blob(db).get("client_id") or settings.google_client_id or "").strip()


def get_client_secret(db: Session) -> str:
    enc = _client_blob(db).get("client_secret_enc")
    if enc:
        try:
            return decrypt(enc) or ""
        except Exception:  # noqa: BLE001
            logger.warning("stored Drive client secret could not be decrypted", exc_info=True)
    return settings.google_client_secret or ""


def set_client(db: Session, *, client_id: str | None, client_secret: str | None) -> None:
    blob = _client_blob(db)
    if client_id is not None:
        blob["client_id"] = client_id.strip() or None
    if client_secret is not None:
        blob["client_secret_enc"] = encrypt(client_secret.strip()) if client_secret.strip() else None
    blob = {k: v for k, v in blob.items() if v}
    row = db.get(AppSetting, CLIENT_KEY)
    if row is None:
        db.add(AppSetting(key=CLIENT_KEY, value=blob))
    else:
        row.value = blob
    db.commit()


def client_hint(db: Session) -> dict[str, Any]:
    """Non-secret view for the Settings page."""
    secret = get_client_secret(db)
    stored = _client_blob(db)
    return {
        "client_id": get_client_id(db),
        "client_secret_masked": (f"••••{secret[-4:]}" if len(secret) >= 4 else ("••••" if secret else None)),
        "source": "settings" if stored.get("client_id") or stored.get("client_secret_enc") else (
            "env" if settings.google_client_id else "none"
        ),
    }


def oauth_configured(db: Session) -> bool:
    return bool(get_client_id(db) and get_client_secret(db))


def _http() -> httpx.AsyncClient:
    # Split out so tests can swap in an httpx.MockTransport.
    return httpx.AsyncClient(timeout=30)


# --------------------------------------------------------------------------- #
# Token storage (encrypted, in app_settings)
# --------------------------------------------------------------------------- #
def load_token(db: Session) -> dict[str, Any] | None:
    row = db.get(AppSetting, TOKEN_KEY)
    if row is None or not isinstance(row.value, dict) or not row.value.get("refresh_enc"):
        return None
    return dict(row.value)


def _save_token(db: Session, *, refresh_token: str | None, access_token: str,
                expires_in: int, scope: str | None) -> None:
    existing = load_token(db) or {}
    refresh_enc = encrypt(refresh_token) if refresh_token else existing.get("refresh_enc")
    payload = {
        "refresh_enc": refresh_enc,
        "access_token": access_token,
        "expires_at": (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=max(0, expires_in - 60))
        ).isoformat(),
        "scope": scope or existing.get("scope") or SCOPE,
    }
    row = db.get(AppSetting, TOKEN_KEY)
    if row is None:
        db.add(AppSetting(key=TOKEN_KEY, value=payload))
    else:
        row.value = payload
    db.commit()


def clear_token(db: Session) -> None:
    row = db.get(AppSetting, TOKEN_KEY)
    if row is not None:
        db.delete(row)
        db.commit()


def is_connected(db: Session) -> bool:
    return load_token(db) is not None


# --------------------------------------------------------------------------- #
# OAuth flow
# --------------------------------------------------------------------------- #
def build_authorize_url(db: Session, state: str) -> str:
    if not oauth_configured(db):
        raise DriveError("Google OAuth не налаштовано — введіть Client ID та Client secret "
                         "у розділі «Google Drive» на сторінці Налаштування (див. DOCS.md)")
    params = httpx.QueryParams({
        "client_id": get_client_id(db),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",       # ask for a refresh token
        "include_granted_scopes": "true",
        "prompt": "consent",            # force a refresh token even on re-consent
        "state": state,
    })
    return f"{AUTH_ENDPOINT}?{params}"


async def exchange_code(db: Session, code: str) -> None:
    data = {
        "client_id": get_client_id(db),
        "client_secret": get_client_secret(db),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri(),
    }
    async with _http() as client:
        resp = await client.post(TOKEN_ENDPOINT, data=data)
    if resp.status_code != 200:
        raise DriveError(f"обмін коду не вдався: {resp.text[:300]}")
    body = resp.json()
    if not body.get("refresh_token"):
        raise DriveError("Google не повернув refresh_token — приберіть доступ застосунку "
                         "в акаунті Google і спробуйте ще раз (потрібен prompt=consent)")
    _save_token(db, refresh_token=body["refresh_token"], access_token=body["access_token"],
                expires_in=int(body.get("expires_in", 3600)), scope=body.get("scope"))


async def _refresh(db: Session, refresh_token: str) -> str:
    data = {
        "client_id": get_client_id(db),
        "client_secret": get_client_secret(db),
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    async with _http() as client:
        resp = await client.post(TOKEN_ENDPOINT, data=data)
    if resp.status_code != 200:
        raise DriveError(f"оновлення токена не вдалося: {resp.text[:300]}")
    body = resp.json()
    _save_token(db, refresh_token=body.get("refresh_token"), access_token=body["access_token"],
                expires_in=int(body.get("expires_in", 3600)), scope=body.get("scope"))
    return body["access_token"]


async def get_access_token(db: Session) -> str:
    tok = load_token(db)
    if tok is None:
        raise DriveError("Google Drive не під'єднано")
    try:
        expires_at = dt.datetime.fromisoformat(tok["expires_at"])
    except (KeyError, ValueError):
        expires_at = dt.datetime.now(dt.timezone.utc)
    if tok.get("access_token") and expires_at > dt.datetime.now(dt.timezone.utc):
        return tok["access_token"]
    refresh_token = decrypt(tok["refresh_enc"])
    return await _refresh(db, refresh_token)


async def revoke(db: Session) -> None:
    tok = load_token(db)
    if tok and tok.get("refresh_enc"):
        try:
            async with _http() as client:
                await client.post(REVOKE_ENDPOINT, params={"token": decrypt(tok["refresh_enc"])})
        except Exception:  # noqa: BLE001 — best effort
            logger.info("Drive token revoke failed", exc_info=True)
    clear_token(db)


# --------------------------------------------------------------------------- #
# Drive REST calls
# --------------------------------------------------------------------------- #
async def _api(db: Session, method: str, url: str, **kw) -> httpx.Response:
    token = await get_access_token(db)
    headers = {"Authorization": f"Bearer {token}", **kw.pop("headers", {})}
    async with _http() as client:
        resp = await client.request(method, url, headers=headers, **kw)
    if resp.status_code == 401:
        raise DriveError("Google відхилив токен — під'єднайте Drive заново")
    if resp.status_code >= 400:
        raise DriveError(f"Drive API {resp.status_code}: {resp.text[:300]}")
    return resp


async def list_folders(db: Session, parent_id: str | None = None) -> list[dict[str, Any]]:
    parent = parent_id or "root"
    q = f"'{parent}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false"
    resp = await _api(db, "GET", f"{API_BASE}/files", params={
        "q": q, "fields": "files(id,name,folderColorRgb)", "pageSize": 200,
        "orderBy": "name", "spaces": "drive",
    })
    return resp.json().get("files", [])


async def create_folder(db: Session, *, name: str, parent_id: str | None,
                        color: str | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        meta["parents"] = [parent_id]
    if color:
        meta["folderColorRgb"] = color
    resp = await _api(db, "POST", f"{API_BASE}/files",
                      params={"fields": "id,name,folderColorRgb"}, json=meta)
    return resp.json()


async def set_folder_color(db: Session, folder_id: str, color: str | None) -> str | None:
    """Set (or reset, when `color` is falsy) a folder's colour on Drive. Google
    snaps to the nearest allowed colour and returns it — that value is echoed
    back so the UI shows what actually stuck."""
    payload = {"folderColorRgb": color or DEFAULT_FOLDER_COLOR}
    resp = await _api(db, "PATCH", f"{API_BASE}/files/{folder_id}",
                      params={"fields": "folderColorRgb"}, json=payload)
    return resp.json().get("folderColorRgb")


async def get_color_palette(db: Session) -> list[str]:
    """The account's live folder-colour palette (falls back to FOLDER_COLORS)."""
    try:
        resp = await _api(db, "GET", "https://www.googleapis.com/drive/v3/about",
                          params={"fields": "folderColorPalette"})
        palette = resp.json().get("folderColorPalette") or []
        return [c.lower() for c in palette] or list(FOLDER_COLORS)
    except DriveError:
        return list(FOLDER_COLORS)


async def upload_file(db: Session, *, data: bytes, name: str,
                      parent_id: str | None, mime: str | None) -> str:
    meta: dict[str, Any] = {"name": name}
    if parent_id:
        meta["parents"] = [parent_id]
    boundary = "receiptscanner-drive-boundary"
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(meta)}\r\n"
        f"--{boundary}\r\nContent-Type: {mime or 'application/octet-stream'}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--".encode()
    resp = await _api(
        db, "POST", f"{UPLOAD_BASE}/files",
        params={"uploadType": "multipart", "fields": "id"},
        headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        content=body,
    )
    return resp.json()["id"]


async def move_and_rename(db: Session, *, file_id: str, name: str,
                          target_parent_id: str | None) -> None:
    # Drive needs the current parents to remove them when re-filing.
    cur = await _api(db, "GET", f"{API_BASE}/files/{file_id}", params={"fields": "parents"})
    old_parents = ",".join(cur.json().get("parents", []))
    params = {"fields": "id"}
    if target_parent_id:
        params["addParents"] = target_parent_id
        if old_parents:
            params["removeParents"] = old_parents
    await _api(db, "PATCH", f"{API_BASE}/files/{file_id}", params=params, json={"name": name})
