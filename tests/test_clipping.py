"""The clip test, which is pure arithmetic and needs no desktop.

A control clipped by a scroll viewport used to be indistinguishable in a listing
from a healthy one: UIA's BoundingRectangle is *already* clipped, so a 44x44
colour swatch cut to four DIPs honestly reports `44x4` - and `44x4` reads as a
deliberately short control unless you happen to know the swatch is square. The
one case where a screenshot of that control is guaranteed useless was the one
case nothing flagged.

The risk in flagging it is false positives, and they all come from the same
place: edges are flush with a viewport's edges all the time for perfectly
ordinary reasons. So the tests below are mostly about what must *not* be called
clipped.
"""

from __future__ import annotations

from isodesk import render
from isodesk.uia import _clipped


def rect(left, top, width, height):
    return {
        "left": left, "top": top, "right": left + width, "bottom": top + height,
        "width": width, "height": height,
    }


WINDOW = rect(0, 0, 1000, 800)
# A vertical scroller sitting mid-content: it can go both up and down.
SCROLLER = {"rect": rect(100, 100, 600, 400), "can_up": True, "can_down": True}
AT_TOP = {"rect": rect(100, 100, 600, 400), "can_up": False, "can_down": True}
AT_BOTTOM = {"rect": rect(100, 100, 600, 400), "can_up": True, "can_down": False}


def test_a_control_wholly_inside_everything_is_not_clipped():
    assert not _clipped(rect(150, 200, 44, 44), WINDOW, [SCROLLER])


def test_a_control_past_the_window_edge_is_clipped_with_no_scroller():
    """The free tier: one window rect, no ScrollPattern probing at all."""
    assert _clipped(rect(950, 200, 200, 44), WINDOW, [])
    assert _clipped(rect(-10, 200, 44, 44), WINDOW, [])


def test_a_swatch_cut_by_the_viewport_bottom_is_clipped():
    """The reported case: 44 DIPs tall, four of them visible."""
    assert _clipped(rect(150, 496, 44, 4), WINDOW, [SCROLLER])


def test_a_row_flush_with_the_top_of_a_scroller_at_the_top_is_not_clipped():
    """The obvious false positive: the first row of an unscrolled list."""
    assert not _clipped(rect(150, 100, 300, 44), WINDOW, [AT_TOP])


def test_a_row_flush_with_the_bottom_of_a_scroller_at_the_bottom_is_not_clipped():
    assert not _clipped(rect(150, 456, 300, 44), WINDOW, [AT_BOTTOM])


def test_full_width_content_in_a_vertical_scroller_is_not_clipped():
    """The false positive the axis restriction exists to prevent.

    A card that fills a vertical scroller's width is flush left and right on
    every single listing, and horizontal is not a scrollable axis here.
    """
    assert not _clipped(rect(100, 200, 600, 44), WINDOW, [SCROLLER])


def test_the_scroller_itself_is_not_clipped_by_its_own_viewport():
    assert not _clipped(rect(100, 100, 600, 400), WINDOW, [SCROLLER])
    # Nor is a panel wrapping it.
    assert not _clipped(rect(50, 50, 700, 500), WINDOW, [SCROLLER])


def test_a_control_outside_the_scroller_is_judged_by_the_window_only():
    assert not _clipped(rect(750, 200, 44, 44), WINDOW, [SCROLLER])


def test_a_missing_rect_is_never_clipped():
    assert not _clipped(None, WINDOW, [SCROLLER])


def test_the_word_reaches_the_compact_row_and_the_json():
    control = {
        "id": "c7", "name": "Accent", "control_type": "Button",
        "xaml_type": "Button", "rect_dips": rect(150, 496, 44, 4),
        "enabled": True, "visible": True, "clipped": True,
    }
    assert "clipped" in render.state_words(control)
    assert render.row(control).endswith("clipped")
    assert render.trim(control, "dips")["clipped"] is True

    del control["clipped"]
    assert "clipped" not in render.state_words(control)
