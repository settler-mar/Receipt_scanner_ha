from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.services.product_images import PRODUCT_IMAGE_DIR

router = APIRouter(prefix="/api/product-images", tags=["product-images"])

_SAFE_FILENAME = re.compile(r"^[0-9a-f]{64}\.(?:avif|gif|jpg|png|webp)$")


@router.get("/{filename}")
def get_product_image(filename: str):
    if not _SAFE_FILENAME.fullmatch(filename):
        raise HTTPException(status_code=404, detail="Product image not found")
    path = PRODUCT_IMAGE_DIR / Path(filename).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Product image not found")
    return FileResponse(path)
