"""Local (no-AI) reading of a receipt's fiscal QR code.

Two halves, deliberately separable: `fiscal_url` knows what a ДПС receipt URL
looks like, and `scan`/`variants`/`decoders` know how to get one out of a phone
photo. Import from here; `app.scanner.fiscal_qr` stays as the historical name.
"""
from app.scanner.qr.fiscal_url import (
    FiscalQr,
    fiscal_identity,
    is_fiscal,
    parse_fiscal_url,
)
from app.scanner.qr.scan import (
    QR_DECODED,
    QR_NONE,
    QR_UNREADABLE,
    QrScan,
    decode_fiscal_qr,
    decode_qr_payloads,
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
