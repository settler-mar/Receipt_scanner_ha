"""Paper-receipt recognition pipeline (ROADMAP.md Крок 3).

Isolated package, same approach as app/models/shopping.py: everything here is
built and tested standalone; the wiring into the upload router / collector
persistence path is a thin layer on top (app/api/inbox.py).

Pipeline order, with early exit as soon as a stage produces a result that
*reconciles* (see reconcile.py):

    1. fiscal QR   — decode the ПРРО/РРО QR, optionally enrich from the tax cabinet
    2. template    — a known retail chain, parsed by a YAML layout spec
    3. generic     — heuristic structural parse of local OCR text
    4. AI (vision) — last resort, via app.services.ai.ocr_receipt

Personal documents never enter this package — the upload router routes them to
app.services.inbox instead (which must not import app.services.ai).
"""
from app.scanner.aspect import (
    ColorStats,
    ImageShape,
    color_stats,
    image_dimensions,
    route,
    route_by_shape,
)
from app.scanner.pipeline import ScanOutcome, scan_receipt_bytes

__all__ = [
    "ColorStats",
    "ImageShape",
    "color_stats",
    "image_dimensions",
    "route",
    "route_by_shape",
    "ScanOutcome",
    "scan_receipt_bytes",
]
