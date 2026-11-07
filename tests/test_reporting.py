"""The screenshot response's crop echo, which is three frames, not one.

The input frame was always screen-absolute and always documented. What was wrong
was everything around it: the echo came back window-relative and post-clamp, in
pixels when the input defaults to DIPs, and describing an image that had since
been downscaled to `max_width`. Three frame changes, none labelled, all reported
as "cropped to 1956x512 px at 171,698 within the window" - which reads as a flat
contradiction of the numbers the caller copied out of a control listing.
"""

from __future__ import annotations

from isodesk.mcp_server import _crop_lines


def test_no_crop_says_nothing():
    assert _crop_lines({"hwnd": 1}) == []


def test_all_three_frames_are_named():
    lines = _crop_lines({
        "crop": {"left": 171, "top": 698, "width": 1956, "height": 512},
        "crop_requested": {
            "left": 342, "top": 1396, "width": 716, "height": 70,
            "units": "dips", "source": "rect of c23",
        },
        "delivered": {"width": 978, "height": 256, "scale": 0.5},
    })
    text = "\n".join(lines)

    # Delivered leads, because it is the only one describing the actual image.
    assert lines[0] == "cropped to 978x256 px delivered (downscaled 0.5x from 1956x512)"
    # The caller's own numbers come back in the caller's own frame and units.
    assert "requested: 342,1396 716x70 dips" in text
    assert "screen-absolute, same frame as the rects in a control listing" in text
    # And the window-relative rect is labelled as such rather than left unmarked.
    assert "in-window: 171,698 1956x512 px" in text


def test_an_undownscaled_crop_does_not_claim_a_resize():
    lines = _crop_lines({
        "crop": {"left": 10, "top": 20, "width": 300, "height": 100},
        "delivered": {"width": 300, "height": 100, "scale": 1.0},
    })
    assert lines[0] == "cropped to 300x100 px delivered"
    assert "downscaled" not in "\n".join(lines)


def test_clamping_is_reported_under_the_frames_it_applies_to():
    lines = _crop_lines({
        "crop": {"left": 0, "top": 780, "width": 60, "height": 4},
        "crop_requested": {
            "left": 100, "top": 900, "width": 44, "height": 44, "units": "dips",
        },
        "delivered": {"width": 60, "height": 4, "scale": 1.0},
        "clamped": "clamped to the window: 9% of the requested region lies inside "
                   "the window bounds",
    })
    assert "9% of the requested region" in "\n".join(lines)
