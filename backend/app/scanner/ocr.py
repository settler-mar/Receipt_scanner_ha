"""Local OCR wrapper used by the template and generic stages (never AI).

Tesseract (`ukr+eng` traineddata) is the default engine — it is a system-image
dependency (Dockerfile), not a pip package, so this module degrades to
``OcrUnavailable`` when the binary or ``pytesseract`` is missing rather than
breaking import of the whole pipeline. The generic/template stages then simply
don't run and the pipeline falls through to AI (or to a QR-only result).

See ROADMAP.md "Відкриті питання" — Tesseract vs PaddleOCR is still to be
compared on real photos; keeping the surface tiny (`run_ocr` → text + word
boxes) makes swapping the engine a one-file change.
"""
from __future__ import annotations

import dataclasses
import io


class OcrUnavailable(RuntimeError):
    """No local OCR engine installed — caller should skip OCR-dependent stages."""


@dataclasses.dataclass
class OcrWord:
    text: str
    left: int
    top: int
    width: int
    height: int
    conf: float


@dataclasses.dataclass
class OcrResult:
    text: str
    words: list[OcrWord]

    def lines(self) -> list[str]:
        return [ln for ln in (l.strip() for l in self.text.splitlines()) if ln]


def _ensure_tesseract():
    try:
        import pytesseract  # noqa: F401
    except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
        raise OcrUnavailable("pytesseract is not installed") from exc
    import pytesseract

    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:  # pragma: no cover - env dependent
        raise OcrUnavailable(f"tesseract binary unavailable: {exc}") from exc
    return pytesseract


def run_ocr(image: bytes, lang: str = "ukr+eng") -> OcrResult:
    pytesseract = _ensure_tesseract()
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(image)) as im:
        im = ImageOps.exif_transpose(im).convert("L")
        data = pytesseract.image_to_data(
            im, lang=lang, output_type=pytesseract.Output.DICT
        )
        text = pytesseract.image_to_string(im, lang=lang)

    words: list[OcrWord] = []
    for i, token in enumerate(data["text"]):
        token = (token or "").strip()
        if not token:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        words.append(OcrWord(
            text=token,
            left=int(data["left"][i]), top=int(data["top"][i]),
            width=int(data["width"][i]), height=int(data["height"][i]),
            conf=conf,
        ))
    return OcrResult(text=text, words=words)


def ocr_available() -> bool:
    try:
        _ensure_tesseract()
        return True
    except OcrUnavailable:
        return False
