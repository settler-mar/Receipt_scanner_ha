"""Gluing overlapping photos of one long receipt into a single image."""
import cv2
import numpy as np
import pytest

from app.scanner import stitch


def _receipt(lines: int = 40, width: int = 600) -> np.ndarray:
    """A tall fake receipt: numbered rows, so an overlap is unambiguous."""
    height = lines * 40
    paper = np.full((height, width, 3), 245, np.uint8)
    for row in range(lines):
        cv2.putText(paper, f"POZYCIA {row:03d} .... {row * 7 + 3}.00",
                    (20, row * 40 + 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (20, 20, 20), 2)
    return paper


def _png(image: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", image)
    assert ok
    return buf.tobytes()


def test_two_overlapping_frames_are_glued_without_repeating_the_overlap():
    paper = _receipt()
    top = _png(paper[:900])
    bottom = _png(paper[700:])          # 200px of overlap

    glued, notes = stitch.stitch([top, bottom])
    image = cv2.imdecode(np.frombuffer(glued, np.uint8), cv2.IMREAD_COLOR)

    scale = stitch.TARGET_WIDTH / paper.shape[1]
    expected = round(paper.shape[0] * scale)
    # The seam removed the repeat, so the result is the whole receipt, not the
    # sum of both frames.
    assert abs(image.shape[0] - expected) < 40
    assert "накладання" in notes[0]


def test_frames_that_do_not_overlap_are_stacked_and_flagged():
    paper = _receipt()
    top = _png(paper[:600])
    unrelated = _png(np.full((400, 600, 3), 255, np.uint8))

    glued, notes = stitch.stitch([top, unrelated])
    image = cv2.imdecode(np.frombuffer(glued, np.uint8), cv2.IMREAD_COLOR)
    assert "спільної ділянки не знайдено" in notes[0]
    # Nothing was dropped: a bad guess must not eat lines.
    scale = stitch.TARGET_WIDTH / 600
    assert image.shape[0] > round(600 * scale)


def test_frames_shot_from_different_distances_still_line_up():
    paper = _receipt()
    top = _png(cv2.resize(paper[:900], None, fx=1.4, fy=1.4))
    bottom = _png(paper[700:])

    glued, notes = stitch.stitch([top, bottom])
    assert "накладання" in notes[0]
    image = cv2.imdecode(np.frombuffer(glued, np.uint8), cv2.IMREAD_COLOR)
    assert image.shape[1] == stitch.TARGET_WIDTH


def test_three_frames_in_order():
    paper = _receipt(60)
    parts = [_png(paper[:900]), _png(paper[700:1600]), _png(paper[1400:])]
    glued, notes = stitch.stitch(parts)
    image = cv2.imdecode(np.frombuffer(glued, np.uint8), cv2.IMREAD_COLOR)
    scale = stitch.TARGET_WIDTH / paper.shape[1]
    assert abs(image.shape[0] - round(paper.shape[0] * scale)) < 60
    assert len(notes) == 2


def test_a_single_frame_comes_back_as_itself():
    glued, notes = stitch.stitch([_png(_receipt(10))])
    assert notes == []
    assert cv2.imdecode(np.frombuffer(glued, np.uint8), cv2.IMREAD_COLOR) is not None


def test_nonsense_input_is_refused():
    with pytest.raises(stitch.StitchError):
        stitch.stitch([])
    with pytest.raises(stitch.StitchError):
        stitch.stitch([b"not an image"])
