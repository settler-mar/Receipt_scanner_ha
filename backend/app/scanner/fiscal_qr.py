"""Historical import path for the QR reader — see `app.scanner.qr`.

The module grew from one decode call into a whole localisation/rectification
ladder and became a package; this re-export keeps the twenty-odd call sites
(pipeline, series_scan, telegram, fiscal_queue, manual_dedup) untouched.
"""
from app.scanner.qr import (  # noqa: F401
    QR_DECODED,
    QR_NONE,
    QR_UNREADABLE,
    FiscalQr,
    QrScan,
    decode_fiscal_qr,
    decode_qr_payloads,
    fiscal_identity,
    is_fiscal,
    parse_fiscal_url,
    scan_images,
    scan_qr,
)

__all__ = [
    "FiscalQr",
    "QR_DECODED",
    "QR_NONE",
    "QR_UNREADABLE",
    "QrScan",
    "decode_fiscal_qr",
    "decode_qr_payloads",
    "fiscal_identity",
    "is_fiscal",
    "parse_fiscal_url",
    "scan_images",
    "scan_qr",
]
