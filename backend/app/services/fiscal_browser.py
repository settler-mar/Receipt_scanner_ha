from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
import secrets
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from patchright.async_api import BrowserContext, Page, Playwright, async_playwright

from app.scanner.fiscal_check import parse_check_xml

TTL = 600
VIEWPORT = {"width": 1400, "height": 900}
PROFILE_DIR = Path(os.environ.get("RS_FISCAL_BROWSER_PROFILE", "/app/data/fiscal-browser-profile"))


@dataclass
class Session:
    id: str
    job_id: str
    expires: float
    pw: Playwright
    context: BrowserContext
    page: Page
    payload: dict | None = None


sessions: dict[str, Session] = {}
_session_lock = asyncio.Lock()


def _profile_has_live_process() -> bool:
    """Return true when a process in this container currently owns the profile."""
    marker = f"--user-data-dir={PROFILE_DIR}"
    proc = Path("/proc")
    with contextlib.suppress(OSError):
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            with contextlib.suppress(OSError):
                command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "ignore")
                if marker in command and "chrome" in command.lower():
                    return True
    return False


def _remove_stale_profile_locks() -> None:
    """Remove Chrome locks left by a stopped/replaced Docker container."""
    if _profile_has_live_process():
        return
    lock = PROFILE_DIR / "SingletonLock"
    if lock.is_symlink():
        # Chrome's symlink target is normally <hostname>-<pid>. A lock from a
        # different container hostname can never represent a local process.
        with contextlib.suppress(OSError):
            target = os.readlink(lock)
            if target.startswith(f"{socket.gethostname()}-"):
                pid_text = target.rsplit("-", 1)[-1]
                if pid_text.isdigit() and Path(f"/proc/{pid_text}").exists():
                    return
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = PROFILE_DIR / name
        with contextlib.suppress(FileNotFoundError, OSError):
            path.unlink()


async def _close_session(session: Session) -> None:
    with contextlib.suppress(Exception):
        await session.context.close()
    with contextlib.suppress(Exception):
        await session.pw.stop()


async def _remove_expired() -> None:
    now = time.monotonic()
    expired = [sid for sid, session in sessions.items() if session.expires <= now or session.page.is_closed()]
    for sid in expired:
        session = sessions.pop(sid, None)
        if session:
            await _close_session(session)


async def start(job_id: str, url: str) -> str:
    """Open one interactive DPS window while retaining ordinary cookies."""
    async with _session_lock:
        await _remove_expired()
        # Chrome locks a persistent profile. The fiscal-check UI therefore has
        # one active browser window and closes an older one before replacement.
        for sid, old in list(sessions.items()):
            sessions.pop(sid, None)
            await _close_session(old)

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _remove_stale_profile_locks()
        sid = secrets.token_urlsafe(24)
        pw = await async_playwright().start()
        try:
            context = await pw.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                headless=False,
                channel="chrome",
                args=["--no-sandbox", "--disable-dev-shm-usage"],
                viewport=VIEWPORT,
                locale="uk-UA",
                timezone_id="Europe/Kyiv",
                color_scheme="light",
            )
        except Exception:
            await pw.stop()
            raise

        page = context.pages[0] if context.pages else await context.new_page()
        session = Session(sid, job_id, time.monotonic() + TTL, pw, context, page)
        sessions[sid] = session

        async def capture(response) -> None:
            if "chkAllWeb" not in response.url or not response.ok:
                return
            with contextlib.suppress(Exception):
                payload = await response.json()
                if isinstance(payload, dict):
                    session.payload = payload

        page.on("response", lambda response: asyncio.create_task(capture(response)))
        page.set_default_timeout(15_000)
        try:
            # Enter through the cabinet origin so the persistent profile gets
            # regular site cookies before opening the fiscal-check form.
            if not page.url.startswith("https://cabinet.tax.gov.ua/"):
                await page.goto("https://cabinet.tax.gov.ua/", wait_until="domcontentloaded", timeout=45_000)
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except Exception:
            # A partially loaded challenge can still be usable remotely.
            if page.is_closed():
                await close(sid)
                raise
        return sid


def is_live(sid: str | None, job_id: str | None = None) -> bool:
    """Whether `sid` still names a usable session in *this* process.

    Sessions live only in `sessions` above, so every id stored in the database
    is dead after a restart. Callers use this to avoid handing the UI a session
    id that can only answer 410.
    """
    session = sessions.get(sid or "")
    if session is None or session.page.is_closed():
        return False
    return job_id is None or session.job_id == job_id


async def get(sid: str, job_id: str) -> Session:
    await _remove_expired()
    session = sessions.get(sid)
    if not session or session.job_id != job_id or session.page.is_closed():
        raise ValueError("Браузерна сесія завершена")
    session.expires = time.monotonic() + TTL
    return session


async def screenshot(sid: str, job_id: str) -> bytes:
    return await (await get(sid, job_id)).page.screenshot(type="jpeg", quality=85)


async def click(sid: str, job_id: str, x: float, y: float) -> None:
    session = await get(sid, job_id)
    await session.page.mouse.click(x, y)
    await asyncio.sleep(0.8)


async def reload(sid: str, job_id: str) -> None:
    session = await get(sid, job_id)
    session.payload = None
    await session.page.reload(wait_until="domcontentloaded", timeout=45_000)
    await asyncio.sleep(1)


async def state(sid: str, job_id: str) -> dict:
    session = await get(sid, job_id)
    text = ""
    with contextlib.suppress(Exception):
        text = await session.page.locator("body").inner_text(timeout=2_000)
    match = re.search(r"(?:Код помилки|error code)\s*:\s*(\d{6})", text, re.IGNORECASE)
    return {
        "url": session.page.url,
        "error_code": match.group(1) if match else None,
        "result_ready": session.payload is not None,
    }


async def result(sid: str, job_id: str, url: str):
    session = await get(sid, job_id)
    if not session.payload:
        return None
    payload = session.payload
    raw = payload.get("checkXml") or payload.get("check")
    if not raw:
        return None
    data = base64.b64decode(raw)
    plain = None
    if payload.get("checkXml") and payload.get("check"):
        plain = base64.b64decode(payload["check"]).decode("cp1251", "replace")
    return parse_check_xml(data, raw_url=url, plaintext=plain).to_normalized_receipt()


async def close(sid: str) -> None:
    session = sessions.pop(sid, None)
    if session:
        await _close_session(session)
