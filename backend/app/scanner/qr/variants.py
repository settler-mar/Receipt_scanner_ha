"""Renderings of one photo, ordered by how much they cost.

A phone photo of a till receipt defeats a plain `detectAndDecode` in a handful
of predictable ways, and each rendering here targets one of them. They are
generated lazily and in three stages, so an ordinary readable receipt still
costs exactly one zxing call on the untouched frame:

    FAST    the frame as shot, plus a downscale when it dwarfs the detector's
            sweet spot
    NORMAL  the code located, cut out, straightened and re-lit — plus the same
            over each colour channel, which is what defeats a shop's stamp
    DEEP    stamp suppression, other ROI scales, rotations, and a ROI rebuilt
            from finder patterns when the detector could not locate anything

Heavy work happens on the QR crop, never on the whole photo: upscaling a 12 MP
frame to chase a 150 px code costs a hundred times more and reads no better.
"""
from __future__ import annotations

import dataclasses
import itertools
import logging
from typing import Any, Iterator

from app.scanner.qr import decoders

logger = logging.getLogger(__name__)

#: Side length a located code is rectified to first, then the DEEP retries.
#: Small enough that binarisation stays local, big enough for the sampling grid.
ROI_SIZE = 700
ROI_EXTRA_SIZES = (400, 1000)
#: Quiet zone: the crop is taken wider than the detected quad, then framed white.
ROI_MARGIN = 0.20
#: Above this the frame is downscaled before the first decode.
MAX_FAST_SIDE = 1600

# Ink is invisible in its own colour channel: a blue shop stamp stamped across
# the QR is near-white in the blue channel, while the black code stays black.
# Grayscale mixes all three and keeps the stamp dark, which is exactly what
# defeats the decoder — so a failed photo is retried channel by channel.
# Names are Ukrainian because they are shown to the user ("прочитано в
# синьому каналі" tells them something coloured sits on the code).
_CHANNELS = ("синьому", "зеленому", "червоному")   # OpenCV order: B, G, R
MAX_RGB = "найсвітлішому"
LUMA = "яскравості"


@dataclasses.dataclass
class Variant:
    """One prepared image plus what to call it if it is the one that decodes."""
    name: str            # telemetry label, e.g. "roi_max_rgb_otsu"
    image: Any
    channel: str = ""    # user-facing colour note, "" for plain grayscale


# --------------------------------------------------------------------------- #
# Pixel helpers
# --------------------------------------------------------------------------- #
def _otsu(arr, invert: bool = False):
    import cv2

    flag = cv2.THRESH_BINARY_INV if invert else cv2.THRESH_BINARY
    return cv2.threshold(arr, 0, 255, flag + cv2.THRESH_OTSU)[1]


def _clahe(arr):
    import cv2

    return cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(arr)


def _unsharp(arr):
    import cv2

    blurred = cv2.GaussianBlur(arr, (0, 0), 2.0)
    return cv2.addWeighted(arr, 1.6, blurred, -0.6, 0)


def _order_quad(quad):
    """Corners as top-left, top-right, bottom-right, bottom-left."""
    import numpy as np

    pts = np.asarray(quad, np.float32).reshape(4, 2)
    total, diff = pts.sum(axis=1), np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(total)], pts[np.argmin(diff)],
                     pts[np.argmax(total)], pts[np.argmax(diff)]], np.float32)


def rectify(arr, quad, size: int = ROI_SIZE, margin: float = ROI_MARGIN):
    """The quad's contents, warped square and framed by a white quiet zone.

    A receipt is never shot flat on, so the code reaches the decoder as a
    trapezoid with a curved edge; undoing the perspective is usually worth more
    than any amount of contrast work. The crop is taken `margin` wider than the
    detection because the quiet zone around the code is part of the symbol —
    and a white border is painted on regardless, for the case where the crop ran
    into the paper's edge and ate it.
    """
    import cv2
    import numpy as np

    src = _order_quad(quad)
    centre = src.mean(axis=0)
    src = centre + (src - centre) * (1.0 + 2 * margin)
    dst = np.array([[0, 0], [size, 0], [size, size], [0, size]], np.float32)
    try:
        matrix = cv2.getPerspectiveTransform(src, dst)
        warped = cv2.warpPerspective(arr, matrix, (size, size), flags=cv2.INTER_CUBIC,
                                     borderMode=cv2.BORDER_REPLICATE)
    except cv2.error:
        return None
    pad = max(8, size // 12)
    return cv2.copyMakeBorder(warped, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)


def _renderings(arr, prefix: str, channel: str = "", brief: bool = False) -> Iterator[Variant]:
    """The contrast ladder for one crop, cheapest first.

    Thermal paper under room light gives a washed-out low-contrast code (CLAHE,
    Otsu), a phone flash gives a bright half and a dark half (adaptive
    threshold, which decides per neighbourhood), and a photo of a screen gives
    soft edges (unsharp). Two adaptive parameter sets, not a grid: the
    combinatorial version costs seconds and decodes nothing more.
    """
    import cv2

    if arr is None or arr.size == 0:
        return
    yield Variant(prefix, arr, channel)
    equalized = _clahe(arr)
    yield Variant(f"{prefix}_clahe", equalized, channel)
    yield Variant(f"{prefix}_otsu", _otsu(arr), channel)
    yield Variant(f"{prefix}_clahe_otsu", _otsu(equalized), channel)
    yield Variant(f"{prefix}_adaptive_gauss_31", cv2.adaptiveThreshold(
        arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10), channel)
    if brief:
        return
    yield Variant(f"{prefix}_unsharp", _unsharp(arr), channel)
    for block, const in ((15, 5),):
        yield Variant(f"{prefix}_adaptive_gauss_{block}", cv2.adaptiveThreshold(
            arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, const), channel)
    yield Variant(f"{prefix}_adaptive_mean_31", cv2.adaptiveThreshold(
        arr, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 31, 10), channel)
    yield Variant(f"{prefix}_otsu_inv", _otsu(equalized, invert=True), channel)


def suppress_stamp(colour_roi):
    """Coloured ink lifted off the code, leaving the neutral black modules.

    A shop's stamp is saturated; the printed QR is not. So the saturated pixels
    — and only those — are pulled up towards the local background, which fades
    the stamp without touching a single module. Deliberately one more variant
    rather than a fix applied to the image: pushed harder this erases parts of
    the code as well, and then nothing downstream could read it.
    """
    import cv2
    import numpy as np

    if colour_roi is None or colour_roi.ndim != 3:
        return None
    hsv = cv2.cvtColor(colour_roi, cv2.COLOR_BGR2HSV)
    saturation = hsv[:, :, 1]
    base = np.max(colour_roi, axis=2)
    background = cv2.medianBlur(base, 9)
    inked = saturation > 60
    out = base.copy()
    out[inked] = np.maximum(base[inked], background[inked])
    return out


# --------------------------------------------------------------------------- #
# Finder-pattern recovery (§6)
# --------------------------------------------------------------------------- #
def finder_quads(arr) -> list:
    """Quads rebuilt from the three concentric squares a QR is anchored by.

    Used only when the detector located nothing at all. A nested-square triple
    is *not* proof of a QR — it is a place worth pointing the decoder at, and
    the decoder is what decides.
    """
    import cv2
    import numpy as np

    binary = cv2.adaptiveThreshold(arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 31, 10)
    contours, hierarchy = cv2.findContours(binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return []
    hierarchy = hierarchy[0]
    marks: list[tuple[float, float, float]] = []
    for index, contour in enumerate(contours):
        depth, child = 0, hierarchy[index][2]
        while child != -1 and depth < 3:       # square in square in square
            depth += 1
            child = hierarchy[child][2]
        if depth < 2:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if min(w, h) < 8 or not 0.6 <= w / h <= 1.6:
            continue
        if cv2.contourArea(contour) < 0.4 * w * h:   # a ring of text, not a block
            continue
        marks.append((x + w / 2.0, y + h / 2.0, float(max(w, h))))
    marks.sort(key=lambda m: -m[2])
    quads = []
    for trio in itertools.combinations(marks[:10], 3):
        quad = _quad_from_finders(trio)
        if quad is not None:
            quads.append(np.asarray(quad, np.float32))
        if len(quads) >= 3:
            break
    return quads


def _quad_from_finders(trio):
    """The symbol implied by three finder patterns, or None if they aren't one.

    The three sit on the corners of a square: one of them is the right-angle
    vertex, and the fourth corner is the vertex's reflection through the other
    two. Sizes must agree — three unrelated nested squares scattered over a
    receipt do not.
    """
    import numpy as np

    sizes = [m[2] for m in trio]
    if max(sizes) > 1.8 * min(sizes):
        return None
    points = [np.array(m[:2], np.float32) for m in trio]
    best = None
    for i in range(3):
        vertex = points[i]
        a, b = [points[j] for j in range(3) if j != i]
        arm_a, arm_b = a - vertex, b - vertex
        len_a, len_b = float(np.linalg.norm(arm_a)), float(np.linalg.norm(arm_b))
        if min(len_a, len_b) < 3 * max(sizes) or max(len_a, len_b) > 1.3 * min(len_a, len_b):
            continue
        cosine = abs(float(arm_a @ arm_b) / (len_a * len_b + 1e-6))
        if cosine > 0.25:                      # not a right angle
            continue
        if best is None or cosine < best[0]:
            best = (cosine, vertex, a, b)
    if best is None:
        return None
    _cos, vertex, a, b = best
    fourth = a + b - vertex
    corners = np.array([vertex, a, fourth, b], np.float32)
    centre = corners.mean(axis=0)
    # Finder centres sit 3.5 modules inside the symbol, so the real edge is a
    # little outside the four points; 1.25 covers that and the quiet zone.
    return centre + (corners - centre) * 1.25


# --------------------------------------------------------------------------- #
# One photo
# --------------------------------------------------------------------------- #
class Photo:
    """One input image and everything derived from it, all computed on demand."""

    def __init__(self, data: bytes, index: int = 0):
        self.data = data
        self.index = index
        self._gray = None
        self._colour = None
        self._quads: list | None = None
        self._finders: list | None = None

    @classmethod
    def load(cls, data: bytes, index: int = 0) -> "Photo | None":
        photo = cls(data, index)
        return photo if photo.gray is not None else None

    @property
    def gray(self):
        import cv2
        import numpy as np

        if self._gray is None:
            self._gray = cv2.imdecode(np.frombuffer(self.data, np.uint8), cv2.IMREAD_GRAYSCALE)
        return self._gray

    @property
    def colour(self):
        import cv2
        import numpy as np

        if self._colour is None:
            self._colour = cv2.imdecode(np.frombuffer(self.data, np.uint8), cv2.IMREAD_COLOR)
        return self._colour

    @property
    def quads(self) -> list:
        """Where the code is, whatever it says. Located on grayscale and, when
        that finds nothing, on max(R,G,B) — a stamp can hide the pattern from
        the detector just as it hides the payload from the decoder."""
        if self._quads is None:
            self._quads = decoders.locate(self.gray)
            if not self._quads:
                lightest = self.max_rgb()
                if lightest is not None:
                    self._quads = decoders.locate(lightest)
        return self._quads

    @property
    def finders(self) -> list:
        if self._finders is None:
            self._finders = finder_quads(self.gray) if not self.quads else []
        return self._finders

    def max_rgb(self):
        """The lightest of the three channels per pixel: coloured ink loses at
        least one channel to the paper behind it, black QR modules lose none."""
        import numpy as np

        colour = self.colour
        if colour is None or colour.ndim != 3:
            return None
        return np.max(colour, axis=2)

    def is_coloured(self) -> bool:
        """Whether splitting this frame by colour can tell us anything new.

        A grayscale scan, a black-and-white screenshot or a photo of plain
        thermal paper has three identical channels, so the whole colour stage
        would redo the grayscale one six times over.

        Measured on a *patch* of the frame, not its average: a stamp covers a
        few percent of a receipt, and any average over all that white paper
        would call the photo neutral and skip the one stage that reads it.
        """
        import numpy as np

        colour = self.colour
        if colour is None or colour.ndim != 3:
            return False
        sample = colour[::4, ::4].astype(np.int16)
        spread = sample.max(axis=2) - sample.min(axis=2)
        return float((spread > 25).mean()) >= 0.002

    def located(self) -> bool:
        """QR present in this frame, on evidence strong enough to tell the user
        to re-shoot it rather than to say there is no code here (§9)."""
        return (any(decoders.quad_is_convincing(q) for q in self.quads)
                or bool(self.finders))

    def score(self) -> float:
        """How promising this frame is, for ordering a multi-photo batch (§8).

        Size of the located code first — a big code is worth far more than a
        sharp small one — then sharpness, so a motion-blurred duplicate of the
        same shot sinks below the usable one.
        """
        import cv2

        if self.gray is None:
            return -1.0
        biggest = max((decoders.quad_side(q) for q in self.quads), default=0.0)
        sharpness = float(cv2.Laplacian(self.gray, cv2.CV_64F).var())
        return biggest * 10.0 + min(sharpness, 500.0) + (50.0 if self.finders else 0.0)

    # -- stages ----------------------------------------------------------- #
    def fast(self) -> Iterator[Variant]:
        import cv2

        arr = self.gray
        yield Variant("original", arr)
        longest = max(arr.shape[:2])
        if longest > MAX_FAST_SIDE:
            scale = MAX_FAST_SIDE / longest
            yield Variant("downscale", cv2.resize(arr, None, fx=scale, fy=scale,
                                                  interpolation=cv2.INTER_AREA))

    def normal(self) -> Iterator[Variant]:
        import cv2

        for n, quad in enumerate(self.quads):
            roi = rectify(self.gray, quad)
            prefix = "roi" if n == 0 else f"roi{n}"
            yield from _renderings(roi, prefix)

        # Nothing located, or located and still unread: the whole frame, re-lit.
        arr = self.gray
        # Whole-frame work stays brief: at 12 MP each adaptive threshold costs
        # more than the entire crop ladder, and the crop is where codes are won.
        yield from _renderings(_clahe(arr), "full_clahe", brief=True)
        height = arr.shape[0]
        footer = arr[height // 2:, :]
        if footer.size:
            # Every ПРРО prints the code in the bottom half; blowing that up is
            # the cheapest approximation of a crop when nothing was located.
            yield Variant("footer_2x", cv2.resize(footer, None, fx=2.0, fy=2.0,
                                                  interpolation=cv2.INTER_CUBIC))
        yield from self._channel_variants()

    def deep(self) -> Iterator[Variant]:
        import cv2

        colour = self.colour
        for quad in self.quads or self.finders:
            if colour is not None and colour.ndim == 3:
                suppressed = suppress_stamp(rectify(colour, quad))
                if suppressed is not None:
                    yield from _renderings(suppressed, "roi_stamp_suppressed", MAX_RGB)
            for size in ROI_EXTRA_SIZES:
                roi = rectify(self.gray, quad, size=size)
                if roi is None:
                    continue
                yield from _renderings(roi, f"roi{size}")
                # Rotations last and on the binarised crop only: both engines
                # handle orientation themselves nearly always, so this is for
                # the rare symbol whose finder patterns read ambiguously.
                if size == ROI_SIZE:
                    binary = _otsu(_clahe(roi))
                    for turns, code in ((1, cv2.ROTATE_90_CLOCKWISE),
                                        (2, cv2.ROTATE_180),
                                        (3, cv2.ROTATE_90_COUNTERCLOCKWISE)):
                        yield Variant(f"roi_rot{turns * 90}", cv2.rotate(binary, code))
        if self.finders and not self.quads:
            for n, quad in enumerate(self.finders):
                roi = rectify(self.gray, quad)
                yield from _renderings(roi, f"finder_roi{n}")

    def _channel_variants(self) -> Iterator[Variant]:
        """The photo split by colour, for a code with a stamp across it.

        Each plane is tried whole and then again as a crop — and the crop is
        located *on that plane*, not on the grayscale quad, because ink that
        blinds the decoder blinds the detector with it: on a stamped receipt
        grayscale often yields no quad at all, or one off to the side.
        """
        import cv2

        colour = self.colour
        if colour is None or colour.ndim != 3 or not self.is_coloured():
            return
        planes: list[tuple[str, str, Any]] = [
            (name, name, colour[:, :, index]) for index, name in enumerate(_CHANNELS)
        ]
        lightest = self.max_rgb()
        if lightest is not None:
            planes.append(("max_rgb", MAX_RGB, lightest))
        planes.append(("hsv_v", LUMA, cv2.cvtColor(colour, cv2.COLOR_BGR2HSV)[:, :, 2]))
        planes.append(("lab_l", LUMA, cv2.cvtColor(colour, cv2.COLOR_BGR2LAB)[:, :, 0]))

        fallback = self.quads[0] if self.quads else None
        for label, channel, plane in planes:
            equalized = _clahe(plane)
            yield Variant(label, plane, channel)
            yield Variant(f"{label}_clahe", equalized, channel)
            yield Variant(f"{label}_clahe_otsu", _otsu(equalized), channel)
            quad = next(iter(decoders.locate(equalized)), fallback)
            if quad is not None:
                yield from _renderings(rectify(plane, quad), f"roi_{label}", channel,
                                       brief=True)
