"""Reusable Patchright session, JSON capture and pagination primitives.

Store adapters supply only URL predicates and payload parsers. Chromium
lifecycle, storage-state rotation, JSON/JSONP handling and "load more"
pagination stay identical across browser-backed integrations.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from patchright.async_api import Browser, BrowserContext, Page, Playwright, Response, async_playwright


@dataclass(frozen=True)
class BrowserSessionConfig:
  locale: str = "en-US"
  viewport_width: int = 1400
  viewport_height: int = 1000
  # Collection runs must work inside Docker without a display server. Any
  # interactive login flow uses its own browser launcher and can stay headed.
  headless: bool = True
  block_heavy_assets: bool = False
  user_agent: str = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
  )


class BrowserSession:
  """Async Patchright context backed by an encrypted-session JSON string."""

  def __init__(self, storage_state_json: str, config: BrowserSessionConfig | None = None):
    self.storage_state = self.parse_storage_state(storage_state_json)
    self.config = config or BrowserSessionConfig()
    self.playwright: Playwright | None = None
    self.browser: Browser | None = None
    self.context: BrowserContext | None = None
    self.page: Page | None = None

  @staticmethod
  def parse_storage_state(value: str) -> dict:
    state = json.loads(value)
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
      raise ValueError("browser storage state must contain a cookies array")
    return state

  async def __aenter__(self) -> "BrowserSession":
    try:
      self.playwright = await async_playwright().start()
      self.browser = await self.playwright.chromium.launch(
        headless=self.config.headless,
        channel="chrome",
        args=["--no-sandbox",
              "--disable-setuid-sandbox",
              "--disable-dev-shm-usage",
              "--use-fake-ui-for-media-stream",
              "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
              "--webrtc-ip-handling-policy=default_public_interface_only",
              # "--disable-blink-features=AutomationControlled"
              ],
      )
      self.context = await self.browser.new_context(
        storage_state=self.storage_state,
        locale=self.config.locale,
        viewport={
          "width": self.config.viewport_width,
          "height": self.config.viewport_height,
        },
        # user_agent=self.config.user_agent,
      )
      if self.config.block_heavy_assets:
        await self.context.route("**/*", self._route_resource)
      self.page = await self.context.new_page()
      return self
    except Exception:
      await self.close()
      raise

  async def __aexit__(self, *_exc_info) -> None:
    await self.close()

  async def close(self) -> None:
    if self.context is not None:
      with contextlib.suppress(Exception):
        await self.context.close()
    if self.browser is not None:
      with contextlib.suppress(Exception):
        await self.browser.close()
    if self.playwright is not None:
      with contextlib.suppress(Exception):
        await self.playwright.stop()

  async def _route_resource(self, route) -> None:
    if route.request.resource_type in {"image", "media", "font"}:
      await route.abort()
    else:
      await route.continue_()

  async def serialized_storage_state(self) -> str:
    if self.context is None:
      raise RuntimeError("browser session has not been started")
    return json.dumps(await self.context.storage_state(), separators=(",", ":"))


class JsonResponseCapture:
  """Collect matching JSON and JSONP responses without blocking Patchright."""

  def __init__(
      self,
      url_matches: Callable[[str], bool],
      payload_matches: Callable[[Any], bool] | None = None,
  ):
    self.url_matches = url_matches
    self.payload_matches = payload_matches
    self.payloads: list[Any] = []
    self._tasks: list[asyncio.Task] = []

  def attach(self, page: Page) -> None:
    page.on("response", self._schedule)

  def _schedule(self, response: Response) -> None:
    self._tasks.append(asyncio.create_task(self._capture(response)))

  async def _capture(self, response: Response) -> None:
    if not self.url_matches(response.url.lower()):
      return
    body = await self.parse_json_or_jsonp(response)
    if body is not None and (self.payload_matches is None or self.payload_matches(body)):
      self.payloads.append(body)

  async def drain(self) -> None:
    if self._tasks:
      pending, self._tasks = self._tasks, []
      await asyncio.gather(*pending, return_exceptions=True)

  async def wait_for_new(self, previous_count: int, page: Page, attempts: int = 40) -> bool:
    for _ in range(attempts):
      await page.wait_for_timeout(250)
      await self.drain()
      if len(self.payloads) > previous_count:
        return True
    return False

  @staticmethod
  async def parse_json_or_jsonp(response: Response) -> dict | list | None:
    try:
      return await response.json()
    except Exception:
      try:
        text = await response.text()
        start = text.find("{")
        end = text.rfind("}")
        return json.loads(text[start:end + 1]) if start >= 0 and end > start else None
      except Exception:
        return None


@dataclass(frozen=True)
class PageInfo:
  has_more: bool
  button_text: str
  page_index: int = 1


@dataclass
class PageCollectionResult:
  records: dict[str, dict]
  reached_end: bool = False
  stopped_on_known: bool = False


class PaginatedRecordCollector:
  """Generic click-to-load pagination with optional known-record stop."""

  def __init__(
      self,
      record_groups: Callable[[Any], Iterable[Iterable[dict]]],
      record_key: Callable[[dict], str | None],
      page_info: Callable[[Any], PageInfo | None],
      max_pages: int = 100,
      click_timeout_ms: int = 10_000,
      response_wait_attempts: int = 40,
  ):
    self.record_groups = record_groups
    self.record_key = record_key
    self.page_info = page_info
    self.max_pages = max_pages
    self.click_timeout_ms = click_timeout_ms
    self.response_wait_attempts = response_wait_attempts

  async def collect(
      self,
      page: Page,
      capture: JsonResponseCapture,
      *,
      paginate: bool,
      known_keys: set[str] | None = None,
      stop_on_known: bool = False,
      wanted_keys: set[str] | None = None,
  ) -> PageCollectionResult:
    known_keys = known_keys or set()
    wanted = set(wanted_keys) if wanted_keys is not None else None
    result = PageCollectionResult(records={})
    processed = 0

    for _ in range(self.max_pages):
      current = capture.payloads[processed:]
      processed = len(capture.payloads)
      for payload in current:
        for group in self.record_groups(payload):
          for record in group:
            key = self.record_key(record)
            if not key:
              continue
            if stop_on_known and key in known_keys:
              result.stopped_on_known = True
              return result
            if wanted is None or key in wanted:
              result.records[key] = record
              if wanted is not None:
                wanted.discard(key)
      if wanted is not None and not wanted:
        return result
      if not paginate:
        return result

      info = self.page_info(capture.payloads[-1]) if capture.payloads else None
      if info is None or not info.has_more:
        result.reached_end = True
        return result
      before = len(capture.payloads)
      more = page.get_by_text(info.button_text, exact=True)
      if not await more.count() or not await more.first.is_visible():
        return result
      await more.first.scroll_into_view_if_needed()
      await more.first.click(timeout=self.click_timeout_ms)
      if not await capture.wait_for_new(before, page, attempts=self.response_wait_attempts):
        return result
    return result
