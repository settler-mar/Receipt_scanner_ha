"""Best-effort local cache for images attached to purchased receipt items."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from app.adapters.base import NormalizedReceipt
from app.core.config import BASE_DIR

logger = logging.getLogger(__name__)

PRODUCT_IMAGE_DIR = BASE_DIR / "data" / "product-images"
LOCAL_IMAGE_PREFIX = "/api/product-images/"
MAX_IMAGE_BYTES = 12 * 1024 * 1024

_CONTENT_EXTENSIONS = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _existing_local_file(url: str | None) -> Path | None:
    if not url or not url.startswith(LOCAL_IMAGE_PREFIX):
        return None
    filename = Path(url.removeprefix(LOCAL_IMAGE_PREFIX)).name
    return PRODUCT_IMAGE_DIR / filename if filename else None


def _cached_source_url(source_url: str) -> str | None:
    digest = hashlib.sha256(source_url.encode()).hexdigest()
    for extension in _CONTENT_EXTENSIONS.values():
        target = PRODUCT_IMAGE_DIR / f"{digest}{extension}"
        if target.is_file() and target.stat().st_size:
            return f"{LOCAL_IMAGE_PREFIX}{target.name}"
    return None


async def localize_product_images(receipts: list[NormalizedReceipt]) -> None:
    """Download every known purchased-product image and rewrite it to a local URL.

    Failures deliberately leave the remote URL in place. A broken CDN must not
    make an otherwise valid receipt collection fail.
    """
    items = [item for receipt in receipts if not receipt.excluded for item in receipt.items]
    await localize_product_items(items)


async def localize_product_items(items: list[object]) -> None:
    """Localize image fields on normalized items or persisted ReceiptItem rows."""
    if not items:
        return

    PRODUCT_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(6)
    downloads: dict[str, asyncio.Task[str | None]] = {}

    async with httpx.AsyncClient(
        timeout=25,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 ReceiptScanner/1.0", "Accept": "image/*"},
    ) as client:
        discoveries: dict[str, asyncio.Task[str | None]] = {}

        async def discover(product_url: str) -> str | None:
            async with semaphore:
                return await _discover_product_image(client, product_url)

        for item in items:
            if item.product_image_url or item.product_image_source_url or not getattr(item, "product_url", None):
                continue
            product_url = item.product_url
            parsed = urlparse(product_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            discoveries.setdefault(product_url, asyncio.create_task(discover(product_url)))

        if discoveries:
            await asyncio.gather(*discoveries.values())
            for item in items:
                product_url = getattr(item, "product_url", None)
                if product_url in discoveries:
                    discovered_url = discoveries[product_url].result()
                    if discovered_url:
                        item.product_image_url = discovered_url

        async def download(source_url: str) -> str | None:
            async with semaphore:
                return await _download_one(client, source_url)

        for item in items:
            local_file = _existing_local_file(item.product_image_url)
            if local_file is not None and local_file.is_file():
                continue

            source_url = item.product_image_source_url or item.product_image_url
            if not source_url or source_url.startswith(LOCAL_IMAGE_PREFIX):
                continue
            parsed = urlparse(source_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue

            item.product_image_source_url = source_url
            cached_url = _cached_source_url(source_url)
            if cached_url:
                item.product_image_url = cached_url
                continue
            task = downloads.get(source_url)
            if task is None:
                task = asyncio.create_task(download(source_url))
                downloads[source_url] = task

        if downloads:
            await asyncio.gather(*downloads.values())

        for item in items:
            source_url = item.product_image_source_url
            if source_url and source_url in downloads:
                local_url = downloads[source_url].result()
                if local_url:
                    item.product_image_url = local_url


async def _download_one(client: httpx.AsyncClient, source_url: str) -> str | None:
    try:
        async with client.stream("GET", source_url) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            extension = _CONTENT_EXTENSIONS.get(content_type)
            if extension is None:
                extension = Path(urlparse(str(response.url)).path).suffix.lower()
                if extension not in set(_CONTENT_EXTENSIONS.values()):
                    return None

            filename = f"{hashlib.sha256(source_url.encode()).hexdigest()}{extension}"
            target = PRODUCT_IMAGE_DIR / filename
            if target.is_file() and target.stat().st_size:
                return f"{LOCAL_IMAGE_PREFIX}{filename}"

            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_IMAGE_BYTES:
                    raise ValueError("product image is too large")
            if not content:
                return None
            if not _looks_like_image(bytes(content), extension):
                raise ValueError("response is not a supported raster image")
            target.write_bytes(content)
            return f"{LOCAL_IMAGE_PREFIX}{filename}"
    except (httpx.HTTPError, OSError, ValueError) as exc:
        logger.warning("Не вдалося зберегти зображення товару %s: %s", source_url, exc)
        return None


async def _discover_product_image(client: httpx.AsyncClient, product_url: str) -> str | None:
    try:
        response = await client.get(product_url)
        response.raise_for_status()
        markup = response.text[:2_000_000]
        for pattern in (
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        ):
            match = re.search(pattern, markup, re.I)
            if match:
                return urljoin(product_url, match.group(1).replace("&amp;", "&"))
    except (httpx.HTTPError, UnicodeError) as exc:
        logger.warning("Не вдалося знайти зображення в картці %s: %s", product_url, exc)
    return None


def _looks_like_image(content: bytes, extension: str) -> bool:
    if extension == ".jpg":
        return content.startswith(b"\xff\xd8\xff")
    if extension == ".png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if extension == ".gif":
        return content.startswith((b"GIF87a", b"GIF89a"))
    if extension == ".webp":
        return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    if extension == ".avif":
        return len(content) >= 12 and content[4:8] == b"ftyp" and b"avif" in content[8:32]
    return False
