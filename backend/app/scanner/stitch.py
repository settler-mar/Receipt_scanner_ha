"""Glue overlapping photos of one long receipt into a single image.

A receipt taller than the camera frame is shot in parts that deliberately repeat
a few lines: the bottom of one photo is the top of the next. Pasting them into
one tall picture lets the rest of the app treat the series exactly like an
ordinary single photo — one QR pass, one OCR pass, one model call.

The seam is found by correlation, not by features: receipt photos of the same
paper differ mostly by a vertical shift, so the bottom strip of one frame is
searched for in the top half of the next (`cv2.matchTemplate`). A weak match
means the frames do not actually overlap — the images are then simply stacked,
which is still one picture and still readable, and the caller is told so.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Frames are scaled to this width before matching and pasting: photos of the
# same receipt are rarely shot from exactly the same distance.
TARGET_WIDTH = 1000
# Height of the strip taken from the bottom of a frame to look for in the next.
STRIP_HEIGHT = 140
# Below this correlation the "overlap" is noise, not the same piece of paper.
MIN_SCORE = 0.45
# A seam is only believed inside the upper part of the next frame: a match found
# near its bottom would mean the two photos are almost the same shot.
SEARCH_FRACTION = 0.75


class StitchError(RuntimeError):
    """The frames could not be combined at all (not images, for instance)."""


def _decode(data: bytes):
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise StitchError("кадр не є зображенням")
    return image


def _to_width(image, width: int = TARGET_WIDTH):
    import cv2

    height = max(1, round(image.shape[0] * width / image.shape[1]))
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def find_seam(upper, lower) -> tuple[int, float]:
    """Where `lower` continues `upper`: (y offset inside `lower`, score).

    The offset is how much of `lower`'s top repeats what is already in `upper`,
    so pasting starts there.
    """
    import cv2

    strip_height = min(STRIP_HEIGHT, upper.shape[0] // 2, lower.shape[0] // 2)
    if strip_height < 20:
        return 0, 0.0
    strip = cv2.cvtColor(upper[-strip_height:], cv2.COLOR_BGR2GRAY)
    search_height = max(strip_height + 1, int(lower.shape[0] * SEARCH_FRACTION))
    field = cv2.cvtColor(lower[:search_height], cv2.COLOR_BGR2GRAY)
    if field.shape[0] <= strip.shape[0]:
        return 0, 0.0
    result = cv2.matchTemplate(field, strip, cv2.TM_CCOEFF_NORMED)
    _min_v, score, _min_l, location = cv2.minMaxLoc(result)
    # `location[1]` is where the repeated strip starts in `lower`; everything up
    # to the end of that strip is what both frames already show.
    return int(location[1]) + strip_height, float(score)


def stitch(frames: list[bytes]) -> tuple[bytes, list[str]]:
    """Combine frames, in the order they were shot, into one JPEG.

    Returns (image, notes). Notes say, per seam, how much overlap was found —
    or that a seam was not found, which is the one case where a line can be
    missing from the result.
    """
    import cv2
    import numpy as np

    if not frames:
        raise StitchError("немає кадрів для склеювання")
    images = [_to_width(_decode(data)) for data in frames]
    if len(images) == 1:
        ok, buf = cv2.imencode(".jpg", images[0], [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            raise StitchError("не вдалося закодувати зображення")
        return buf.tobytes(), []

    notes: list[str] = []
    canvas = images[0]
    for index, image in enumerate(images[1:], start=2):
        offset, score = find_seam(canvas, image)
        if score < MIN_SCORE or offset >= image.shape[0]:
            notes.append(f"кадр {index}: спільної ділянки не знайдено "
                         f"(збіг {score:.2f}) — кадри складено встик")
            offset = 0
        else:
            notes.append(f"кадр {index}: накладання {offset}px (збіг {score:.2f})")
        canvas = np.vstack([canvas, image[offset:]])

    ok, buf = cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise StitchError("не вдалося закодувати склеєне зображення")
    return buf.tobytes(), notes
