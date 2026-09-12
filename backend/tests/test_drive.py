"""Google Drive client + inbox integration (ROADMAP.md Крок 3)."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import models  # noqa: F401
from app.models import scanner as scanner_models  # noqa: F401
from app.services import drive


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = Session()
    yield s
    s.close()


@pytest.fixture()
def google(monkeypatch):
    monkeypatch.setattr(drive.settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(drive.settings, "google_client_secret", "test-secret")
    monkeypatch.setattr(drive.settings, "base_url", "http://host:8001")


def _mock(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture()
def no_env_client(monkeypatch):
    monkeypatch.setattr(drive.settings, "google_client_id", "")
    monkeypatch.setattr(drive.settings, "google_client_secret", "")


def test_oauth_not_configured_blocks_authorize_url(db, no_env_client):
    assert not drive.oauth_configured(db)
    with pytest.raises(drive.DriveError):
        drive.build_authorize_url(db, "state123")


def test_client_creds_stored_in_settings_win_over_env(db, monkeypatch):
    monkeypatch.setattr(drive.settings, "google_client_id", "env-id")
    monkeypatch.setattr(drive.settings, "google_client_secret", "env-secret")
    assert drive.oauth_configured(db)
    assert drive.client_hint(db)["source"] == "env"

    drive.set_client(db, client_id="ui-id", client_secret="ui-secret")
    assert drive.get_client_id(db) == "ui-id"
    assert drive.get_client_secret(db) == "ui-secret"
    hint = drive.client_hint(db)
    assert hint["source"] == "settings"
    assert hint["client_secret_masked"] == "••••cret" and "ui-secret" not in str(hint)


def test_authorize_url_has_offline_access(db, google):
    url = drive.build_authorize_url(db, "state123")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "access_type=offline" in url and "prompt=consent" in url
    assert "redirect_uri=http%3A%2F%2Fhost%3A8001%2Fapi%2Finbox%2Fdrive%2Fcallback" in url
    assert "state=state123" in url


def test_exchange_and_refresh_cycle(db, google, monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        body = dict(httpx.QueryParams(request.content.decode()))
        if body.get("grant_type") == "authorization_code":
            return httpx.Response(200, json={
                "access_token": "acc-1", "refresh_token": "ref-1", "expires_in": 3600,
                "scope": drive.SCOPE,
            })
        if body.get("grant_type") == "refresh_token":
            assert body["refresh_token"] == "ref-1"
            return httpx.Response(200, json={"access_token": "acc-2", "expires_in": 3600})
        return httpx.Response(400)

    monkeypatch.setattr(drive, "_http", _mock(handler))

    asyncio.run(drive.exchange_code(db, "the-code"))
    assert drive.is_connected(db)

    tok = drive.load_token(db)
    assert tok["access_token"] == "acc-1"
    assert "ref-1" not in json.dumps(tok)  # refresh token stored encrypted, not plaintext

    # force expiry → next get_access_token refreshes
    tok["expires_at"] = "2000-01-01T00:00:00+00:00"
    db.get(models.AppSetting, drive.TOKEN_KEY).value = tok
    db.commit()
    assert asyncio.run(drive.get_access_token(db)) == "acc-2"


def test_list_and_create_folder(db, google, monkeypatch):
    _seed_token(db)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/drive/v3/files":
            assert "in parents" in request.url.params["q"]
            return httpx.Response(200, json={"files": [{"id": "f1", "name": "Документи"}]})
        if request.method == "POST" and request.url.path == "/drive/v3/files":
            meta = json.loads(request.content)
            return httpx.Response(200, json={"id": "new1", "name": meta["name"]})
        return httpx.Response(404)

    monkeypatch.setattr(drive, "_http", _mock(handler))
    assert asyncio.run(drive.list_folders(db)) == [{"id": "f1", "name": "Документи"}]
    made = asyncio.run(drive.create_folder(db, name="Договори", parent_id="f1"))
    assert made["id"] == "new1"


def test_set_folder_color_and_palette(db, google, monkeypatch):
    _seed_token(db)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH" and request.url.path == "/drive/v3/files/fid":
            seen["body"] = json.loads(request.content)
            # Google snaps to the nearest allowed colour
            return httpx.Response(200, json={"folderColorRgb": "#16a765"})
        if request.method == "GET" and request.url.path == "/drive/v3/about":
            return httpx.Response(200, json={"folderColorPalette": ["#AC725E", "#16A765"]})
        return httpx.Response(404)

    monkeypatch.setattr(drive, "_http", _mock(handler))

    applied = asyncio.run(drive.set_folder_color(db, "fid", "#17a866"))
    assert seen["body"] == {"folderColorRgb": "#17a866"}
    assert applied == "#16a765"

    # reset → default colour is sent
    asyncio.run(drive.set_folder_color(db, "fid", None))
    assert seen["body"] == {"folderColorRgb": drive.DEFAULT_FOLDER_COLOR}

    assert asyncio.run(drive.get_color_palette(db)) == ["#ac725e", "#16a765"]


def test_upload_and_move(db, google, monkeypatch):
    _seed_token(db)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/upload/drive/v3/files" in str(request.url):
            seen["ctype"] = request.headers["content-type"]
            return httpx.Response(200, json={"id": "up1"})
        if request.method == "GET" and request.url.path.endswith("/files/up1"):
            return httpx.Response(200, json={"parents": ["old"]})
        if request.method == "PATCH":
            seen["addParents"] = request.url.params.get("addParents")
            seen["removeParents"] = request.url.params.get("removeParents")
            return httpx.Response(200, json={"id": "up1"})
        return httpx.Response(404)

    monkeypatch.setattr(drive, "_http", _mock(handler))
    fid = asyncio.run(drive.upload_file(db, data=b"hello", name="x.pdf",
                                        parent_id="inbox", mime="application/pdf"))
    assert fid == "up1"
    assert seen["ctype"].startswith("multipart/related")
    asyncio.run(drive.move_and_rename(db, file_id="up1", name="Final.pdf", target_parent_id="dst"))
    assert seen["addParents"] == "dst" and seen["removeParents"] == "old"


def _seed_token(db):
    from app.core.security import encrypt

    db.add(models.AppSetting(key=drive.TOKEN_KEY, value={
        "refresh_enc": encrypt("ref"), "access_token": "acc",
        "expires_at": "2999-01-01T00:00:00+00:00", "scope": drive.SCOPE,
    }))
    db.commit()
