"""Verify the third cut - lean listings, scoping, measure, scroll-to - against a real app.

Covers what a layout-parity session actually does: navigate, scope a listing to
the page instead of the whole window, bring a control into view, compare two
controls' geometry, and crop a capture to one of them.

    python tests/smoke_lean_scope.py
    python tests/smoke_lean_scope.py --aumid "..."
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins", "isodesk", "server"))

from isodesk import SessionManager  # noqa: E402
from isodesk import render  # noqa: E402

DEFAULT_AUMID = "Microsoft.WinUI3ControlsGallery_8wekyb3d8bbwe!App"


def _area(control) -> int:
    rect = control.get("rect_dips") or control.get("rect") or {}
    return rect.get("width", 0) * rect.get("height", 0)


def _biggest(controls):
    """The largest control that is not part of the window frame."""
    return max([c for c in controls if not c.get("caption_button")], key=_area)


def _first_scrollable(session, hwnd, listing):
    """Scroll the first of the largest controls that has a scroller in reach.

    `scroll_control` walks to the nearest scrollable, so a caller names what it
    can see - but on a page where nothing scrolls at all there is nothing to
    test here, and that has to read as a skip rather than a failure.
    """
    candidates = sorted(listing["controls"], key=_area, reverse=True)[:6]
    for candidate in candidates:
        try:
            return candidate["id"], session.call(
                "scroll_control", hwnd=hwnd, control_id=candidate["id"],
                direction="down", amount=40, listing=listing["listing"],
            )
        except Exception:
            continue
    return None, None


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aumid", default=DEFAULT_AUMID)
    args = parser.parse_args()

    manager = SessionManager()
    session = manager.create(label="lean")
    failures = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' :: ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    try:
        session.call("activate", aumid=args.aumid, wait=20)
        time.sleep(2.0)
        windows = session.call("list_windows")["windows"]
        hwnd = next(w for w in windows if not w.get("popup"))["hwnd"]

        # -- A: response shape ------------------------------------------------
        section("A - lean response shape")
        listing = session.call("get_controls", hwnd=hwnd)
        compact = render.listing(listing, units="dips", fmt="compact")
        fat = render.listing(listing, units="both", fmt="json")
        ratio = len(compact) / len(fat)
        print(compact[:600])
        print(f"  compact {len(compact)} chars | both/json {len(fat)} chars | ratio {ratio:.2f}")
        check("compact is well under half the JSON", ratio < 0.4, f"{ratio:.2f}")
        check("listing token issued", bool(listing.get("listing")), str(listing.get("listing")))
        check(
            "no row spends characters on enabled/visible when true",
            "enabled" not in compact and "visible" not in compact,
        )
        rendered_ids = {line.split(" ", 1)[0] for line in compact.splitlines()[1:]}
        check(
            "every listed control appears as a row",
            all(c["id"] in rendered_ids for c in listing["controls"]),
        )

        # -- B: scoping and filtering ----------------------------------------
        section("B - scoping and server-side filters")
        full_count = listing["count"]
        # The biggest non-caption control is the page or navigation root, which is
        # what a real session scopes to; a caption button has no subtree at all.
        scope_target = _biggest(listing["controls"])
        scoped = session.call("get_controls", hwnd=hwnd, scope=scope_target["id"])
        print(f"  full {full_count} rows -> scope={scope_target['id']} "
              f"({scope_target.get('xaml_type')}) {scoped['count']} rows")
        check("scoped listing is a subset", scoped["count"] <= full_count)
        check("scoped listing has its own token",
              scoped.get("listing") != listing.get("listing"))

        buttons = session.call("get_controls", hwnd=hwnd, control_type="Button")
        types = {c["control_type"] for c in buttons["controls"]}
        print(f"  control_type=Button -> {buttons['count']} rows, types {types}")
        check("control_type filter is exact", types <= {"Button"})
        check("filters echoed back", buttons.get("filters", {}).get("control_type") == "Button")

        named = session.call("get_controls", hwnd=hwnd, name_contains="e")
        check("name_contains filters", all("e" in c["name"].casefold()
                                           for c in named["controls"]))

        # A superseded id must be refused, not resolved against another control.
        stale_refused = False
        try:
            session.call("click_control", hwnd=hwnd,
                         control_id=listing["controls"][0]["id"],
                         listing=listing["listing"])
        except Exception as exc:
            stale_refused = "listing" in str(exc)
        check("an id quoted against a superseded listing is refused", stale_refused)

        # -- C: targeted re-read ---------------------------------------------
        section("C - refresh_controls")
        current = session.call("get_controls", hwnd=hwnd)
        ids = [c["id"] for c in current["controls"][:3]]
        refreshed = session.call("refresh_controls", hwnd=hwnd, ids=ids,
                                 listing=current["listing"])
        text = render.refreshed(refreshed)
        print(text)
        check("every id came back", len(refreshed["rows"]) == len(ids))
        check("rows are live, not stale",
              not any(r.get("stale") for r in refreshed["rows"]))
        check("refresh is a fraction of a listing", len(text) < len(compact) / 2,
              f"{len(text)} vs {len(compact)}")

        # -- D: measure -------------------------------------------------------
        section("D - measure")
        pair = [c["id"] for c in current["controls"][:2]]
        measured = session.call("measure", hwnd=hwnd, control_ids=pair,
                                listing=current["listing"])
        text = render.measurement(measured)
        print(text)
        check("one pair of deltas", len(measured["pairs"]) == 1)
        first, second = (
            next(c for c in current["controls"] if c["id"] == pair[0]),
            next(c for c in current["controls"] if c["id"] == pair[1]),
        )
        expected_dx = second["rect_dips"]["left"] - first["rect_dips"]["left"]
        check("dx matches the listing arithmetic",
              abs(measured["pairs"][0]["dx"] - expected_dx) <= 1,
              f"{measured['pairs'][0]['dx']} vs {expected_dx}")
        check("measure is a rounding error next to a listing", len(text) < 400)

        # -- E: scrolling -----------------------------------------------------
        section("E - scroll offsets and scroll_to_control")
        scroller_id, scrolled = _first_scrollable(session, hwnd, current)
        check("a scrollable container was reachable", scrolled is not None)
        if scrolled is None:
            scrolled = {}
        print(f"  method {scrolled.get('method')} | notes {scrolled.get('notes')}")
        print(f"  vertical_percent {scrolled.get('vertical_percent')} "
              f"at_top {scrolled.get('at_top')} at_bottom {scrolled.get('at_bottom')}")
        check("amount=40 is clamped rather than run 40 times",
              any("clamped" in note for note in scrolled.get("notes", [])))
        check("scroll position reported", "vertical_percent" in scrolled)

        jumped = session.call("scroll_control", hwnd=hwnd, control_id=scroller_id,
                              to_percent=100) if scroller_id else {}
        print(f"  to_percent=100 -> {jumped.get('vertical_percent')} "
              f"at_bottom {jumped.get('at_bottom')}")
        check("to_percent reaches the bottom in one call", jumped.get("at_bottom") is True)

        after_scroll = session.call("get_controls", hwnd=hwnd)
        # Something that is neither the scroller itself nor window furniture -
        # the case the old advice ("use select_control") could not cover.
        target = next(
            (c for c in after_scroll["controls"]
             if not c.get("caption_button") and c["id"] != scroller_id),
            after_scroll["controls"][0],
        )
        brought = session.call("scroll_to_control", hwnd=hwnd,
                               control_id=target["id"], listing=after_scroll["listing"])
        print(f"  scroll_to_control({target['id']} {target.get('xaml_type')}) "
              f"-> {brought.get('method')} in_view {brought.get('in_view')}")
        check("scroll_to_control reports the route it took", bool(brought.get("method")))
        check("scroll_to_control answers in_view without a screenshot",
              "in_view" in brought or brought.get("scroller") is None)

        # -- F: cropped capture ----------------------------------------------
        section("F - cropped screenshot")
        listing_now = session.call("get_controls", hwnd=hwnd)
        crop_target = listing_now["controls"][0]
        full = session.call("screenshot", hwnd=hwnd, max_width=1400)
        cropped = session.call("screenshot", hwnd=hwnd,
                               control_id=crop_target["id"], padding=8,
                               listing=listing_now["listing"])
        full_bytes = len(base64.b64decode(full["image_base64"]))
        crop_bytes = len(base64.b64decode(cropped["image_base64"]))
        print(f"  full {full_bytes} bytes | cropped {crop_bytes} bytes "
              f"| region {cropped.get('crop')}")
        check("crop region reported", bool(cropped.get("crop")))
        check("cropped image is materially smaller", crop_bytes < full_bytes / 2,
              f"{crop_bytes} vs {full_bytes}")

        # -- G: orphan discovery ---------------------------------------------
        section("G - orphan discovery")
        orphans = manager.orphans()
        print(f"  {len(orphans)} orphaned desktop(s): "
              f"{[o['session_id'] for o in orphans]}")
        check("this live session is not reported as an orphan",
              all(o["session_id"] != session.id for o in orphans))

    finally:
        section("teardown")
        print(f"  {manager.destroy(session.id)}")

    section("result")
    if failures:
        print(f"  {len(failures)} FAILED: {failures}")
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
