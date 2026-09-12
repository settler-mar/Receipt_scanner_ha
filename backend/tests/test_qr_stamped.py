"""Decoding a QR that a shop stamped over.

Shops stamp receipts with blue ink, sometimes straight across the fiscal QR. In
grayscale that ink is dark and the code dies with it — but ink is nearly
invisible in its own colour channel, so the photo is retried channel by channel.
"""
import cv2
import numpy as np
import pytest

from app.scanner.fiscal_qr import scan_qr

URL = ("https://cabinet.tax.gov.ua/cashregs/check?"
       "id=1&fn=4001118996&date=20260912&time=121046&sm=9253.67")


def _qr_photo() -> np.ndarray:
    code = cv2.QRCodeEncoder.create().encode(URL)
    big = cv2.resize(code, None, fx=10, fy=10, interpolation=cv2.INTER_NEAREST)
    canvas = np.full((big.shape[0] + 80, big.shape[1] + 80), 255, np.uint8)
    canvas[40:40 + big.shape[0], 40:40 + big.shape[1]] = big
    return cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR)


def _stamped(colour=(150, 45, 25)) -> np.ndarray:
    """Blue ink over the code: ink absorbs, so each channel is scaled by it —
    white paper turns blue, black modules stay black."""
    photo = _qr_photo().astype(np.float32)
    height, width = photo.shape[:2]
    mask = np.zeros((height, width), np.uint8)
    cv2.ellipse(mask, (width // 2, height // 2), (width // 3, height // 6), 20, 0, 360, 255, 18)
    cv2.putText(mask, "SPLACHENO", (50, height // 2 + 10),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, 255, 8)
    ink = np.array(colour, np.float32) / 255.0
    stamped = photo.copy()
    for channel in range(3):
        stamped[:, :, channel] = np.where(mask > 0, photo[:, :, channel] * ink[channel],
                                          photo[:, :, channel])
    return stamped.astype(np.uint8)


def _png(image: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", image)
    assert ok
    return buf.tobytes()


def test_a_clean_code_still_decodes_from_the_plain_photo():
    result = scan_qr(_png(_qr_photo()))
    assert result.state == "decoded"
    assert result.payloads[0] == URL
    # No colour trickery was needed, and the answer says so.
    assert result.channel == ""


def test_a_blue_stamp_across_the_code_is_read_in_the_blue_channel():
    result = scan_qr(_png(_stamped()))
    assert result.state == "decoded"
    assert result.payloads[0] == URL
    assert result.channel == "синьому"


def test_the_same_stamp_is_hopeless_once_the_colour_is_thrown_away():
    """Why the channels are needed at all: grayscale mixes the ink back in."""
    grayscale = cv2.cvtColor(_stamped(), cv2.COLOR_BGR2GRAY)
    assert scan_qr(_png(grayscale)).state != "decoded"


@pytest.mark.parametrize("ink", [
    (150, 45, 25),     # blue
    (40, 140, 40),     # green
    (40, 45, 160),     # red
], ids=["blue", "green", "red"])
def test_a_stamp_of_any_ink_colour_still_decodes(ink):
    """Blue is the one that actually blinds grayscale (it carries the least
    luminance), but green and red stamps are read too — whether straight from
    the photo or from a channel."""
    result = scan_qr(_png(_stamped(ink)))
    assert result.state == "decoded"
    assert result.payloads[0] == URL
