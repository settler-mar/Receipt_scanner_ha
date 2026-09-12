"""Global OAuth-browser callback endpoint (silpo.py and any future oauth_browser
store) — this is the redirect_uri registered with the provider, so it's keyed by
store_code + the `state` param round-tripped from accounts.oauth_start, not by
account id directly. See app/core/oauth_state.py."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.adapters.base import AdapterError
from app.adapters.registry import get_adapter
from app.api.deps import get_db
from app.core import oauth_state
from app.core.credentials import upsert_credential
from app.models.models import Account, AccountStatus

router = APIRouter(prefix="/api/oauth", tags=["oauth"])


def _page(title: str, ok: bool) -> HTMLResponse:
    color = "#4ade80" if ok else "#f87171"
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<style>body{{font-family:-apple-system,sans-serif;background:#0f172a;color:#e2e8f0;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
div{{text-align:center}}h2{{color:{color}}}</style></head>
<body><div><h2>{title}</h2><p>Можна закрити цю вкладку.</p></div></body></html>"""
    return HTMLResponse(html, status_code=200 if ok else 400)


@router.get("/{store_code}/callback")
async def oauth_callback(
    store_code: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: Session = Depends(get_db),
):
    if error:
        return _page(f"Помилка авторизації: {error_description or error}", ok=False)
    if not code or not state:
        return _page("Відсутній code або state у відповіді", ok=False)

    pending = oauth_state.pop(state)
    if pending is None:
        return _page("Сесія авторизації застаріла — спробуйте ще раз", ok=False)

    account = db.get(Account, pending["account_id"])
    if account is None:
        return _page("Акаунт не знайдено", ok=False)

    adapter = get_adapter(store_code)
    try:
        result = await adapter.oauth_exchange(code, pending["verifier"], pending["redirect_uri"])
    except AdapterError as exc:
        account.status = AccountStatus.error
        account.last_error = str(exc)
        db.commit()
        return _page(str(exc), ok=False)

    upsert_credential(db, account, result)
    account.status = AccountStatus.active
    account.last_error = None
    db.commit()
    return _page(f"✅ Вхід у {account.label} виконано", ok=True)
