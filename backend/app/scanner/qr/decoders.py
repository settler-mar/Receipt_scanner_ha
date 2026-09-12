"""The two local QR engines, plus the localization half of the job.

Reading a payload and finding where the code sits are different problems, and
the second is far easier than the first (§3.4 of the scanner plan): a photo shot
from too far away still shows three finder squares long after the data modules
stop resolving. So a failed decode is not the end — the coordinates it leaves
behind are what the rectify/enhance ladder in `variants.py` works on.

Engine order never changes: zxing-cpp first because it is both faster and
stronger, opencv behind it so a platform with no zxing wheel still scans.
Adding a third decoder is deliberately not done here — see §7 of the plan: it
waits on a benchmark over real problem receipts.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Below this, a "located" code is too small for its corners to mean anything.
MIN_QR_SIDE_PX = 20


def _decode_zxing(arr) -> list[str]:
    """Read the image with zxing-cpp, the stronger of the two engines.

    Measured on the project's sample photos it decodes every code opencv does
    and several it does not — a dense QR on wrinkled thermal paper, in
    particular — while taking ~0.03s on the untouched frame. Optional purely so
    that a platform without a wheel degrades to opencv instead of breaking.
    """
    try:
        import zxingcpp
    except ImportError:
        return []
    try:
        results = zxingcpp.read_barcodes(arr, formats=zxingcpp.BarcodeFormat.QRCode)
    except Exception:  # noqa: BLE001 — never let a decoder crash the scan
        # Loud, because losing the primary decoder to an API change would
        # otherwise look like receipts that simply stopped having QR codes.
        logger.warning("zxing-cpp decode failed, falling back to opencv", exc_info=True)
        return []
    return [r.text for r in results if r.text]


def _decode_one(detector, arr) -> list[str]:
    """Both opencv decode entry points against a single prepared image."""
    import cv2

    found: list[str] = []
    try:
        ok, decoded, _pts, _straight = detector.detectAndDecodeMulti(arr)
        if ok:
            found.extend(s for s in decoded if s)
    except cv2.error:
        pass
    if not found:
        try:
            single, _pts, _straight = detector.detectAndDecode(arr)
            if single:
                found.append(single)
        except cv2.error:
            pass
    return found


def detector():
    import cv2

    return cv2.QRCodeDetector()


ZXING = "zxing"
OPENCV = "opencv"


def decode(arr, engine: str = ZXING, *, _detector=None) -> tuple[str, list[str]]:
    """(engine name, payloads) for one prepared rendering. Empty name = nothing.

    One engine per call because the two cost wildly different amounts: measured
    over the full variant ladder of a photo that never decodes, zxing spends
    0.14s and opencv 7.1s. Running both on every rendering made a hopeless photo
    take eight seconds before the pipeline could even reach the AI route, so
    `scan.py` sweeps the whole ladder with zxing and only then gives opencv a
    bounded second pass.
    """
    if engine == ZXING:
        payloads = _decode_zxing(arr)
        return (ZXING, payloads) if payloads else ("", [])
    payloads = _decode_one(_detector if _detector is not None else detector(), arr)
    return (OPENCV, payloads) if payloads else ("", [])


def decode_curved(arr) -> list[str]:
    """opencv's curved-symbol path, for a QR printed on a rolled-up receipt.

    Only present in some builds, and it is slow, so it sits alone in the DEEP
    stage rather than in the variant ladder.
    """
    import cv2

    det = detector()
    try:
        single, _pts, _straight = det.detectAndDecodeCurved(arr)
    except (cv2.error, AttributeError):
        return []
    return [single] if single else []


def locate(arr) -> list:
    """Every QR-shaped quad opencv can find in the image, as (4, 2) float32.

    `detectMulti` first: a receipt can carry an advert QR next to the fiscal one,
    and taking only the first detection would send the whole ladder after the
    wrong code.
    """
    import cv2
    import numpy as np

    det = detector()
    quads: list = []
    for call in (lambda: det.detectMulti(arr), lambda: det.detect(arr)):
        try:
            ok, points = call()
        except cv2.error:
            continue
        if ok and points is not None and len(points):
            quads.extend(np.asarray(points, np.float32).reshape(-1, 4, 2))
            break
    return [q for q in quads if quad_side(q) >= MIN_QR_SIDE_PX]


def quad_side(quad) -> float:
    """Length of the shortest edge — the scale at which the code was shot."""
    import numpy as np

    pts = np.asarray(quad, np.float32)
    edges = [float(np.linalg.norm(pts[i] - pts[(i + 1) % 4])) for i in range(4)]
    return min(edges)


def quad_is_convincing(quad) -> bool:
    """Whether this detection is strong enough to call the photo "QR present".

    A single weak contour must not turn QR_NONE into QR_UNREADABLE (§9), so a
    quad only counts when it is big enough to be a real symbol and square enough
    not to be a line of text or a receipt border the detector latched onto.
    """
    import numpy as np

    pts = np.asarray(quad, np.float32)
    edges = [float(np.linalg.norm(pts[i] - pts[(i + 1) % 4])) for i in range(4)]
    shortest, longest = min(edges), max(edges)
    if shortest < MIN_QR_SIDE_PX:
        return False
    return longest <= 3.0 * shortest
