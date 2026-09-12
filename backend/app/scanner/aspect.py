"""Upload routing by image shape + colourfulness (ROADMAP.md Крок 3 "Логіка роутингу").

Two signals, both thresholds configurable per deployment (``inbox_config``) since
they have to be tuned on real scans:

1. **Shape** — a photographed till receipt is a narrow vertical strip
   (``height / width >= receipt_ratio``); a personal document is close to a page
   ratio. Not enough on its own: an ID card / driver's licence is receipt-width
   but must never land in the receipt pipeline.
2. **Colour** — a thermal receipt is near-monochrome (very low saturation, few
   non-gray pixels). A document (licence, passport, a stamped contract) is
   colourful.

Rule:
  * near-monochrome  → **receipt**, whatever the shape (strongest signal —
    a grey thermal strip looks like this every time);
  * otherwise        → by shape.
"""
from __future__ import annotations

import dataclasses
import io
from typing import Literal

# Default: treat anything at least twice as tall as it is wide as a receipt.
DEFAULT_RECEIPT_RATIO = 2.0
# A thermal receipt sits well under these; a colour document sits well over one.
DEFAULT_COLOR_MAX_SATURATION = 0.12   # mean per-pixel saturation (0..1)
DEFAULT_COLOR_MAX_NONGRAY_FRAC = 0.10  # fraction of pixels visibly off-gray

RouteKind = Literal["receipt", "document"]


@dataclasses.dataclass(frozen=True)
class ImageShape:
    width: int
    height: int

    @property
    def ratio(self) -> float:
        """Height / width. Portrait > 1, landscape < 1."""
        if self.width <= 0:
            return 0.0
        return self.height / self.width


@dataclasses.dataclass(frozen=True)
class ColorStats:
    saturation_mean: float      # 0..1
    nongray_fraction: float     # 0..1
    distinct_colors: int        # after 4-bit/channel quantisation (informational)

    def is_monochrome(
        self,
        max_saturation: float = DEFAULT_COLOR_MAX_SATURATION,
        max_nongray_fraction: float = DEFAULT_COLOR_MAX_NONGRAY_FRAC,
    ) -> bool:
        return (
            self.saturation_mean <= max_saturation
            and self.nongray_fraction <= max_nongray_fraction
        )


def image_dimensions(data: bytes) -> ImageShape:
    """(width, height) of an encoded image, honouring the EXIF orientation flag
    so a phone photo taken in portrait isn't misread as landscape."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)
        return ImageShape(width=im.width, height=im.height)


def color_stats(data: bytes, sample_edge: int = 200) -> ColorStats:
    """Cheap colourfulness metrics over a downscaled copy of the image."""
    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((sample_edge, sample_edge))
        arr = np.asarray(im, dtype=np.float32)

    hi = arr.max(axis=2)
    lo = arr.min(axis=2)
    chroma = hi - lo                       # 0..255
    with np.errstate(divide="ignore", invalid="ignore"):
        sat = np.where(hi > 0, chroma / hi, 0.0)   # HSV saturation, 0..1

    nongray = float((chroma > 25).mean())
    quant = (arr.astype(np.uint16) >> 4)
    distinct = int(len(np.unique(quant[:, :, 0] * 289 + quant[:, :, 1] * 17 + quant[:, :, 2])))

    return ColorStats(
        saturation_mean=float(sat.mean()),
        nongray_fraction=nongray,
        distinct_colors=distinct,
    )


def route_by_shape(shape: ImageShape, receipt_ratio: float = DEFAULT_RECEIPT_RATIO) -> RouteKind:
    """Narrow/tall → receipt pipeline; anything else → personal-document inbox."""
    return "receipt" if shape.ratio >= receipt_ratio else "document"


def route(
    shape: ImageShape,
    color: ColorStats | None = None,
    *,
    receipt_ratio: float = DEFAULT_RECEIPT_RATIO,
    color_max_saturation: float = DEFAULT_COLOR_MAX_SATURATION,
    color_max_nongray_fraction: float = DEFAULT_COLOR_MAX_NONGRAY_FRAC,
) -> RouteKind:
    """Combined routing: monochrome always wins for "receipt", else fall back to
    shape. `color` may be None (colour signal unavailable) — then shape decides."""
    if color is not None and color.is_monochrome(color_max_saturation, color_max_nongray_fraction):
        return "receipt"
    return route_by_shape(shape, receipt_ratio)
