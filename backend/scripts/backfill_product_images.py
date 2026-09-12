"""Populate and localize purchased-product images from already saved raw receipts."""
from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from urllib.parse import urljoin, urlparse

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal, ensure_schema_upgrades
from app.models.models import Receipt
from app.services.product_images import localize_product_items

_IMAGE_KEYS = (
    "product_image_url", "product_photo", "lagerImageUrl", "mainImage",
    "imageUrl", "picture", "photoUrl", "image",
)
_NAME_KEYS = ("name", "lagerName", "productName", "title")


def _normalized(value: object) -> str:
    return " ".join(re.findall(r"[a-zа-яіїєґ0-9]+", str(value or "").lower()))


def _walk(value: object) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _candidates(raw: dict) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for row in _walk(raw):
        name = next((row.get(key) for key in _NAME_KEYS if row.get(key)), None)
        image = next((row.get(key) for key in _IMAGE_KEYS if row.get(key)), None)
        if not name or not isinstance(image, str):
            continue
        if image.startswith("//"):
            image = "https:" + image
        elif image.startswith("/"):
            # Only raw payloads from these stores use relative product images.
            image = urljoin("https://dnipro-m.ua", image)
        if urlparse(image).scheme in {"http", "https"}:
            found.append((_normalized(name), image))
    return found


async def main() -> None:
    ensure_schema_upgrades()
    with SessionLocal() as db:
        receipts = db.execute(select(Receipt).options(selectinload(Receipt.items))).scalars().all()
        sourced = 0
        items_to_download = []
        for receipt in receipts:
            candidates = _candidates(receipt.raw or {})
            for item in receipt.items:
                if not item.product_image_url:
                    item_name = _normalized(item.name)
                    matches = [url for name, url in candidates if name == item_name]
                    if not matches and len(receipt.items) == 1 and len(candidates) == 1:
                        matches = [candidates[0][1]]
                    if matches:
                        item.product_image_url = matches[0]
                        sourced += 1
                if item.product_image_url:
                    items_to_download.append(item)

        await localize_product_items(items_to_download)
        db.commit()
        localized = sum(1 for item in items_to_download if (item.product_image_url or "").startswith("/api/product-images/"))
        print(f"sources_added={sourced} localized={localized} candidates={len(items_to_download)}")


if __name__ == "__main__":
    asyncio.run(main())
