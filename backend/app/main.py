from contextlib import asynccontextmanager
import asyncio
import contextlib
import datetime as dt
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.api import (accounts, categories, dashboard, duplicates, fiscal_queue, inbox, oauth, product_images,
                     products, receipts, settings as settings_api, shopping_list,
                     stores, telegram_log, voice, warranties, widgets)
from app.core.config import settings
from app.db import Base, engine, ensure_schema_upgrades, run_migrations
from app.mcp_server import mcp_app
from app.models import models  # noqa: F401 — registers tables on Base.metadata
from app.models import scanner as scanner_models  # noqa: F401 — Крок 3 tables on Base.metadata
from app.models import shopping as shopping_models  # noqa: F401 — Крок 2 tables on Base.metadata
from app.scheduler import start_scheduler
from app.seed import seed_categories, seed_stores
from app.services.telegram import polling_loop as telegram_polling_loop


_STARTED_AT = dt.datetime.now(dt.timezone.utc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_schema_upgrades()
    # Stored scan frames used to live inside the package; move any that
    # are still there onto the data directory the deployment mounts.
    inbox.migrate_legacy_scan_inputs()
    run_migrations()
    seed_stores()
    seed_categories()
    start_scheduler()
    telegram_task = asyncio.create_task(telegram_polling_loop(), name="telegram-bot")
    # mcp_app is mounted at /mcp; its lifespan runs the MCP session manager and
    # isn't started automatically for a mounted sub-app, so enter it here.
    async with mcp_app.router.lifespan_context(mcp_app):
        try:
            yield
        finally:
            telegram_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await telegram_task


app = FastAPI(title="Receipt Scanner", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=settings.cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(stores.router)
app.include_router(accounts.router)
app.include_router(categories.router)
app.include_router(receipts.router)
app.include_router(dashboard.router)
app.include_router(widgets.router)
app.include_router(products.router)
app.include_router(product_images.router)
app.include_router(warranties.router)
app.include_router(oauth.router)
app.include_router(settings_api.router)
app.include_router(inbox.router)
app.include_router(fiscal_queue.router)
app.include_router(shopping_list.router)
app.include_router(duplicates.router)
app.include_router(voice.router)
app.include_router(telegram_log.router)

# Own MCP server (ROADMAP.md Крок 4) — Streamable HTTP at /mcp, LAN-only like
# the REST API. Mounted after every /api router so it can't shadow one.
app.mount("/mcp", mcp_app)


@app.get("/api/health")
def health():
    now = dt.datetime.now(dt.timezone.utc)
    uptime_seconds = int((now - _STARTED_AT).total_seconds())
    return {
        "status": "ok",
        "started_at": _STARTED_AT.isoformat(),
        "uptime_seconds": uptime_seconds,
        "uptime": str(dt.timedelta(seconds=uptime_seconds)),
    }


# The production container embeds the Vite build here. This catch-all is added
# after every API router, so /api routes keep precedence while frontend routes
# and static files work both at / and behind Home Assistant Ingress.
_frontend_dist = Path(
    os.environ.get("RS_FRONTEND_DIST", str(Path(__file__).resolve().parent.parent / "frontend_dist"))
).resolve()


if (_frontend_dist / "index.html").is_file():
    @app.get("/{full_path:path}", include_in_schema=False)
    async def frontend(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API endpoint not found")

        candidate = (_frontend_dist / full_path).resolve()
        if _frontend_dist == candidate or _frontend_dist in candidate.parents:
            if candidate.is_file():
                return FileResponse(candidate)
        return FileResponse(_frontend_dist / "index.html")
