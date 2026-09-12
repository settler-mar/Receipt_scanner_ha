"""Back-compat shim.

The real implementation moved to `fiscal_check.py`. It prefers the documented
token-authenticated `chkAll` API, can optionally fall back to the browser's
`chkAllWeb` endpoint, and breaks the result down line by line ("рознесення").
The pipeline still imports this module and calls `fetch_structured(fiscal_qr)`.
"""
from __future__ import annotations

from app.scanner.fiscal_check import fetch_check, fetch_structured, parse_check_xml

__all__ = ["fetch_structured", "fetch_check", "parse_check_xml"]
