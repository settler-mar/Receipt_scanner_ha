"""The QR ladder on the photos that defeat a plain decode.

One case per rung of app/scanner/qr: a code that only comes back once the
perspective is undone, one that only comes back in a colour channel, a receipt
carrying two codes, and a batch where only one frame is usable. Each is built so
that `cv2.QRCodeDetector().detectAndDecode` on the frame as shot fails — that is
what makes them worth the cost of the stage they exercise.
"""
import cv2
import numpy as np

from app.scanner.qr import QR_DECODED, QR_NONE, scan_images, scan_qr

FISCAL = ("https://cabinet.tax.gov.ua/cashregs/check?"
          "id=1234567890&fn=4000000002&date=20260912&time=121046&sm=9253.67")
ADVERT = "https://example.com/promo/autumn"


def _code(payload: str, px: int) -> np.ndarray:
    return cv2.resize(cv2.QRCodeEncoder.create().encode(payload), (px, px),
                      interpolation=cv2.INTER_NEAREST)


def _receipt(*codes, size=(1400, 800)) -> np.ndarray:
    """A receipt-shaped page carrying the given (code, top, left) placements."""
    page = np.full(size, 210, np.uint8)
    for code, top, left in codes:
        page[top:top + code.shape[0], left:left + code.shape[1]] = code
    return cv2.cvtColor(page, cv2.COLOR_GRAY2BGR)


def _stamped(image: np.ndarray, ink=(150, 45, 25)) -> np.ndarray:
    """Blue ink over the lower half: it absorbs, so every channel is scaled by
    it — white paper turns blue while the black modules stay black."""
    height, width = image.shape[:2]
    mask = np.zeros((height, width), np.uint8)
    cv2.ellipse(mask, (width // 2, 1000), (width // 4, 90), 12, 0, 360, 255, 12)
    out = image.astype(np.float32)
    tint = np.array(ink, np.float32) / 255.0
    for channel in range(3):
        out[:, :, channel] = np.where(mask > 0, out[:, :, channel] * tint[channel],
                                      out[:, :, channel])
    return out.astype(np.uint8)


def _jpeg(image: np.ndarray, quality: int = 80) -> bytes:
    return cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])[1].tobytes()


def _plain_decode_fails(data: bytes) -> bool:
    arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    return not cv2.QRCodeDetector().detectAndDecode(arr)[0]


def test_a_receipt_shot_at_an_angle_is_straightened_before_decoding():
    """Undoing the perspective is worth more than any amount of contrast work:
    nobody holds a phone square to a receipt lying on a counter."""
    page = _receipt((_code(FISCAL, 260), 950, 270))
    height, width = page.shape[:2]
    skewed = cv2.warpPerspective(
        page,
        cv2.getPerspectiveTransform(
            np.float32([[0, 0], [width, 0], [width, height], [0, height]]),
            np.float32([[width * .34, 0], [width * .93, height * .03],
                        [width, height], [0, height * .96]])),
        (width, height), borderValue=(235, 235, 235))
    result = scan_qr(_jpeg(skewed))

    assert result.state == QR_DECODED
    assert result.fiscal.document_id == "1234567890"


def test_a_stamp_across_the_code_is_lifted_by_a_colour_plane():
    """Grayscale mixes the ink back in; the plane where the ink is lightest
    keeps the black modules and loses the stamp."""
    stamped = _stamped(_receipt((_code(FISCAL, 300), 880, 250)))
    data = _jpeg(stamped)
    assert _plain_decode_fails(data)

    result = scan_qr(data)
    assert result.state == QR_DECODED
    assert result.fiscal is not None
    # Which rung won is telemetry, not behaviour — test_qr_stamped.py pins the
    # colour-plane path itself, on ink heavy enough that nothing else can.
    assert result.variant


def test_an_advert_code_does_not_end_the_search_for_the_fiscal_one():
    """A receipt carries loyalty and advert QRs beside the fiscal one. Stopping
    at the first payload that decodes would file the receipt with no ДПС
    document at all — so a non-fiscal code is kept aside, not returned."""
    page = _receipt((_code(ADVERT, 220), 120, 290), (_code(FISCAL, 300), 880, 250))
    result = scan_qr(_jpeg(_stamped(page)))

    assert result.state == QR_DECODED
    assert result.fiscal is not None and result.fiscal.document_id == "7738620256"


def test_a_non_fiscal_code_is_still_reported_when_it_is_all_there_is():
    result = scan_qr(_jpeg(_receipt((_code(ADVERT, 300), 900, 250))))

    assert result.state == QR_DECODED
    assert result.payloads == [ADVERT]
    assert result.fiscal is None


def test_the_usable_frame_of_a_batch_is_the_one_that_decodes():
    """A long receipt is shot in parts and the code lands in exactly one of
    them; the scan names that frame so the batch can be treated as one receipt."""
    blank = _jpeg(_receipt())
    blurred = _jpeg(cv2.GaussianBlur(_receipt((_code(FISCAL, 90), 900, 350)), (21, 21), 0))
    good = _jpeg(_receipt((_code(FISCAL, 300), 880, 250)))

    result = scan_images([blank, blurred, good])
    assert result.state == QR_DECODED
    assert result.image_index == 2
    assert result.fiscal.document_id == "7738620256"


def test_a_page_with_no_code_is_reported_as_none_not_unreadable():
    """QR_NONE and QR_UNREADABLE lead to opposite advice, so a weak contour must
    never be promoted into "there is a code here, re-shoot it"."""
    result = scan_qr(_jpeg(_receipt()))
    assert result.state == QR_NONE
    assert result.payloads == []


def test_a_successful_scan_records_how_it_was_won():
    """Telemetry (§12): which engine, which rendering, which frame, how long —
    enough to reorder the ladder later by what actually pays."""
    result = scan_qr(_jpeg(_receipt((_code(FISCAL, 300), 880, 250))))

    assert result.state == QR_DECODED
    assert result.decoder in ("zxing", "opencv")
    assert result.variant
    assert result.elapsed_ms >= 0
