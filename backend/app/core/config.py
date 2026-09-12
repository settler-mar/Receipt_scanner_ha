from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_prefix="RS_")

    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'receipts.db'}"
    # Fernet key for encrypting stored tokens at rest. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    secret_key: str = "CHANGE_ME_dev_only_insecure_key_______________="
    # Regex, not a fixed list — this is a LAN tool with no login of its own, opened
    # from whatever device/IP is on the network at the time (and the server's own
    # LAN IP isn't stable across reconnects either, see base_url below). Matches
    # localhost/127.0.0.1 and the three private-network ranges (RFC 1918) on any
    # port, so it covers phones/laptops on the same Wi-Fi without hand-listing IPs
    # — and still excludes the public internet, unlike a bare "*" origin.
    cors_origin_regex: str = (
        r"^https?://(localhost|127\.0\.0\.1"
        r"|192\.168\.\d{1,3}\.\d{1,3}"
        r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})"
        r"(:\d+)?$"
    )
    # This backend's own externally-reachable base URL — used to build OAuth
    # redirect_uri values (silpo.py). Must exactly match what's registered with
    # the provider's Dynamic Client Registration (see adapters/silpo.py).
    # Override per deployment via RS_BASE_URL in backend/.env (see .env.example)
    # — on a LAN that is the host's own IP, e.g. http://192.168.1.50:8001.
    base_url: str = "http://localhost:8001"

    # Google OAuth client for the document-inbox Drive integration (ROADMAP Крок 3).
    # App-registration level, set once per deployment — see DOCS.md. The user then
    # connects their own Drive through the Settings page; that refresh token is
    # stored encrypted in app_settings, not here. Redirect URI registered with
    # Google must be exactly {base_url}/api/inbox/drive/callback.
    google_client_id: str = ""
    google_client_secret: str = ""


settings = Settings()
(BASE_DIR / "data").mkdir(exist_ok=True)
