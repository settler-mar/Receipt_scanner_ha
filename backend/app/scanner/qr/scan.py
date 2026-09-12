"""Decode the Ukrainian fiscal (ПРРО/РРО) QR from one photo or a batch of them.

Ukrainian software/hardware fiscal registrars print a QR that points at the tax
authority's public receipt viewer, e.g.

    https://cabinet.tax.gov.ua/cashregs/check?id=95123&date=20260830&time=194512&fn=4000123456&sum=347.20

so decoding it is worth a lot of effort: it turns a photo into the ДПС document
itself rather than a reading of the paper. No AI is involved anywhere here.

The ladder runs FAST over every frame, then NORMAL, then DEEP, best frame first
(§8) — a batch is one receipt, and the frame where the code came out biggest and
sharpest is the one worth spending on. Any stage stops the moment a *fiscal* URL
decodes. A non-fiscal payload does not stop anything (§3.3): receipts carry
advert and loyalty QRs beside the fiscal one, and quitting at the first of those
would lose the only code that matters.
"""
from __future__ import annotations

import dataclasses
import itertools
import logging
import time
from typing import Iterable, Iterator

from app.scanner.qr import decoders
from app.scanner.qr.fiscal_url import FiscalQr, parse_fiscal_url
from app.scanner.qr.variants import Photo, Variant

logger = logging.getLogger(__name__)

#: What a photo's QR turned out to be — reported to the user verbatim, because
#: "no QR here" and "a QR I can see but not read" need opposite advice.
QR_NONE = "none"              # nothing QR-shaped in the frame
QR_UNREADABLE = "unreadable"  # located, but too small/blurred to decode
QR_DECODED = "decoded"        # decoded, though not necessarily a fiscal URL

#: Wall-clock ceiling on everything past FAST. The ladder is ordered by expected
#: value, so cutting it off costs the least-likely variants first — and an
#: upload that hangs for a minute on one hopeless photo is worse than a scan
#: that falls through to the AI route a second earlier.
SCAN_BUDGET_S = 8.0

#: How far into the ladder the slower engine is allowed to follow — enough for
#: the frame as shot and every rendering of the located code.
OPENCV_VARIANTS = 24


@dataclasses.dataclass
class QrScan:
    state: str
    payloads: list[str] = dataclasses.field(default_factory=list)
    # Which rendering finally decoded it ("" = the plain grayscale photo). Shown
    # in the scan notes: "прочитано в синьому каналі" tells the user the stamp
    # over the code was worked around, not that the photo was fine.
    channel: str = ""
    # Telemetry (§12): enough to reorder the ladder later by what actually pays.
    decoder: str = ""
    variant: str = ""
    image_index: int = 0
    elapsed_ms: int = 0

    @property
    def fiscal(self) -> "FiscalQr | None":
        for payload in self.payloads:
            parsed = parse_fiscal_url(payload)
            if parsed is not None:
                return parsed
        return None


def scan_qr(image: bytes) -> QrScan:
    """Decode one photo's QR codes, and say why when that fails."""
    return scan_images([image])


def scan_images(images: Iterable[bytes], *, budget: float = SCAN_BUDGET_S) -> QrScan:
    """The same, over the frames of one receipt shot in several photos.

    The result describes the batch: `image_index` names the frame that decoded.
    """
    started = time.monotonic()
    photos = [p for p in (Photo.load(data, i) for i, data in enumerate(images))
              if p is not None]
    if not photos:
        return QrScan(QR_NONE)

    detector = decoders.detector()
    # A decoded but non-fiscal code: kept, reported if nothing better turns up,
    # and otherwise ignored so the search continues.
    aside: QrScan | None = None

    def attempt(photo: Photo, variant: Variant, engine: str) -> QrScan | None:
        nonlocal aside
        if variant.image is None:
            return None
        name, payloads = decoders.decode(variant.image, engine, _detector=detector)
        if not payloads:
            return None
        found = QrScan(QR_DECODED, list(payloads), channel=variant.channel,
                       decoder=name, variant=variant.name, image_index=photo.index)
        if found.fiscal is not None:
            return found
        if aside is None:
            aside = found
        return None

    def walk(stream: Iterator[Variant], photo: Photo, engine: str) -> QrScan | None:
        for variant in stream:
            hit = attempt(photo, variant, engine)
            if hit is not None:
                return hit
            if time.monotonic() - started > budget:
                logger.info("QR ladder hit its %.1fs budget on image %d at %r",
                            budget, photo.index, variant.name)
                return None
        return None

    for photo in photos:
        hit = walk(photo.fast(), photo, decoders.ZXING)
        if hit is not None:
            return _finish(hit, started)

    # Past FAST every stage is expensive, so the best frame goes first: on a
    # long receipt shot in parts the code is legible in exactly one of them.
    ranked = sorted(photos, key=lambda p: -p.score())
    for stage in ("normal", "deep"):
        for photo in ranked:
            if time.monotonic() - started > budget:
                break
            hit = walk(getattr(photo, stage)(), photo, decoders.ZXING)
            if hit is not None:
                return _finish(hit, started)
            if stage == "deep":
                payloads = decoders.decode_curved(photo.gray)
                if payloads and any(parse_fiscal_url(p) for p in payloads):
                    return _finish(QrScan(QR_DECODED, payloads, decoder=decoders.OPENCV,
                                          variant="curved", image_index=photo.index), started)

    # Second engine, second pass. opencv exists so a platform with no zxing
    # wheel still scans, and it does occasionally read a rendering zxing does
    # not — but at ~70ms per call it cannot be spent on all ~100 renderings, so
    # it gets the head of the ladder: the frame as shot and the located code.
    for photo in ranked:
        if time.monotonic() - started > budget:
            break
        head = itertools.chain(photo.fast(), photo.normal())
        hit = walk(itertools.islice(head, OPENCV_VARIANTS), photo, decoders.OPENCV)
        if hit is not None:
            return _finish(hit, started)

    if aside is not None:
        return _finish(aside, started)
    located = any(photo.located() for photo in photos)
    return _finish(QrScan(QR_UNREADABLE if located else QR_NONE), started)


def _finish(scan: QrScan, started: float) -> QrScan:
    scan.elapsed_ms = int((time.monotonic() - started) * 1000)
    if scan.state == QR_DECODED:
        logger.info("QR decoded: decoder=%s image=%d variant=%s elapsed_ms=%d",
                    scan.decoder, scan.image_index, scan.variant, scan.elapsed_ms)
    else:
        # No image content in the log — only the verdict and what it cost.
        logger.info("QR not decoded: state=%s elapsed_ms=%d", scan.state, scan.elapsed_ms)
    return scan


def decode_qr_payloads(image: bytes) -> list[str]:
    """Every QR string we can decode from the image (0..n)."""
    return scan_qr(image).payloads


def decode_fiscal_qr(image: bytes) -> FiscalQr | None:
    """Convenience: first tax-cabinet QR in the image, parsed."""
    return scan_qr(image).fiscal
