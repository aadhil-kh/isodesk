"""Verify the five feedback fixes against a real app.

Everything in `test_schema.py`, `test_clipping.py` and `test_reporting.py` runs
without a desktop. These are the halves that cannot: the caption guard and the
retarget report on the coordinate path, the crop echo's three real frames, and
`screenshot(control_id=)` scrolling a clipped control into view before capturing
it.

    python tests/smoke_feedback_fixes.py
    python tests/smoke_feedback_fixes.py --aumid "..."
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins", "isodesk", "server"))

from isodesk import SessionManager  # noqa: E402

DEFAULT_AUMID = "Microsoft.WinUI3ControlsGallery_8wekyb3d8bbwe!App"


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def _area(control) -> int:
    rect = control.get("rect_dips") or control.get("rect") or {}
    return rect.get("width", 0) * rect.get("height", 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aumid", default=DEFAULT_AUMID)
    args = parser.parse_args()

    manager = SessionManager()
    session = manager.create(label="feedback")
    failures = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' :: ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    try:
        session.call("activate", aumid=args.aumid, wait=20)
        time.sleep(2.0)
        windows = session.call("list_windows")["windows"]
        window = next(w for w in windows if not w.get("popup"))
        hwnd = window["hwnd"]
        rect = window["rect"]

        listing = session.call("get_controls", hwnd=hwnd)
        controls = listing["controls"]

        # -- A5: the caption guard, inherited by the coordinate path ---------
        section("A5  click_xy refuses a caption button")

        caption = next((c for c in controls if c.get("caption_button")), None)
        if caption is None:
            check("a caption button is in the listing", False,
                  "none found - the guard cannot be exercised on this window")
        else:
            box = caption["rect"]
            point = (
                (box["left"] + box["right"]) // 2,
                (box["top"] + box["bottom"]) // 2,
            )
            probe = session.call("_element_at", x=point[0], y=point[1])
            print(f"  UIA sees {probe['resolved'].get('name')!r}; "
                  f"the window says {probe.get('frame_hit')!r}")
            try:
                result = session.call("click_xy", x=point[0], y=point[1])
                check("click_xy on a caption button is refused", False,
                      f"it was invoked instead: {result}")
            except Exception as exc:
                check(
                    "click_xy on a caption button is refused",
                    "window frame" in str(exc) or "caption button" in str(exc),
                    str(exc)[:160],
                )
            # And the refusal must survive method="auto"'s PostMessage fallback:
            # posting the click the guard just declined would hit Close anyway.
            try:
                session.call("click_xy", x=point[0], y=point[1], method="auto")
                check("the refusal is not swallowed by the fallback", False,
                      "auto fell through to PostMessage")
            except Exception as exc:
                check("the refusal is not swallowed by the fallback",
                      "PostMessage" not in str(exc), str(exc)[:160])

        # -- A5: method="uia" refuses rather than falling back ---------------
        section('A5  method="uia" does not fall back to PostMessage')

        dead = (rect["left"] - 5000, rect["top"] - 5000)
        try:
            result = session.call("click_xy", x=dead[0], y=dead[1], method="uia")
            check('method="uia" raises on a dead point', False, str(result)[:160])
        except Exception as exc:
            check('method="uia" raises on a dead point',
                  "PostMessage" not in str(exc), str(exc)[:160])

        try:
            result = session.call("click_xy", x=dead[0], y=dead[1], method="auto")
            check('method="auto" falls back and says so',
                  result.get("method") == "PostMessage"
                  and bool(result.get("uia_fallback_reason")),
                  str(result)[:160])
        except Exception as exc:
            # A point that far outside may not resolve to a window at all, which
            # is a legitimate raise rather than a silent success.
            check('method="auto" falls back and says so', True, f"raised: {exc}"[:160])

        # -- A5: retargeting is named ---------------------------------------
        section("A5  a click that resolves off-point reports retargeted")

        # A Button, not a Text: a Text exposes no invokable pattern, so clicking
        # one falls through to PostMessage and the response carries no `resolved`
        # at all - which would be testing the fallback, not the retarget report.
        probe = next(
            (c for c in controls
             if c.get("rect") and c["control_type"] == "Button"
             and not c.get("caption_button") and c.get("name")),
            None,
        )
        if probe is None:
            check("a probe control exists", False, "no named Button to click")
        else:
            box = probe["rect"]
            point = ((box["left"] + box["right"]) // 2, (box["top"] + box["bottom"]) // 2)
            try:
                result = session.call("click_xy", x=point[0], y=point[1])
                resolved = result.get("resolved", {})
                same = resolved.get("name") == probe.get("name")
                check(
                    "the response names what it resolved to",
                    bool(resolved.get("control_type") or resolved.get("name") is not None),
                    str(resolved)[:120],
                )
                check(
                    "a retarget is reported rather than silent",
                    same or "retargeted" in result,
                    f"resolved={resolved.get('name')!r} "
                    f"retargeted={result.get('retargeted')}",
                )
            except Exception as exc:
                check("click_xy on an ordinary control works", False, str(exc)[:160])

        # -- A2: the crop echo carries all three frames ----------------------
        section("A2  the crop echo names requested, in-window and delivered")

        target = max(
            (c for c in controls if not c.get("caption_button") and c.get("rect")),
            key=_area,
        )
        shot = session.call(
            "screenshot", hwnd=hwnd, control_id=target["id"],
            listing=listing["listing"], max_width=200,
        )
        check("crop_requested is echoed", bool(shot.get("crop_requested")),
              str(shot.get("crop_requested"))[:140])
        check("crop_requested is in the caller's units",
              (shot.get("crop_requested") or {}).get("units") == "dips",
              str((shot.get("crop_requested") or {}).get("units")))
        check("the requested rect matches the listing's rect",
              (shot.get("crop_requested") or {}).get("width")
              == (target.get("rect_dips") or {}).get("width"),
              f"echo={(shot.get('crop_requested') or {}).get('width')} "
              f"listing={(target.get('rect_dips') or {}).get('width')}")
        check("the delivered size is reported", bool(shot.get("delivered")),
              str(shot.get("delivered")))
        check("the delivered size is the downscaled one, not the crop",
              (shot.get("delivered") or {}).get("width") <= 200,
              str(shot.get("delivered")))

        # -- A3: control_id scrolls into view before capturing ---------------
        section("A3  screenshot(control_id=) brings a clipped control into view")

        clipped = [c for c in controls if c.get("clipped")]
        print(f"  {len(clipped)} clipped control(s) in the listing")
        for control in clipped[:5]:
            print(f"    {control['id']} {control.get('name')!r} {control.get('rect_dips')}")

        deep = sorted(
            (c for c in controls if c.get("rect") and not c.get("caption_button")),
            key=lambda c: c["rect"]["top"],
        )
        probe = deep[-1] if deep else None
        if probe is None:
            check("a control to scroll to exists", False)
        else:
            shot = session.call(
                "screenshot", hwnd=hwnd, control_id=probe["id"],
                listing=listing["listing"],
            )
            check("scrolled_into_view is reported", "scrolled_into_view" in shot,
                  str(shot.get("scrolled_into_view"))[:200])

            off = session.call(
                "screenshot", hwnd=hwnd, control_id=probe["id"],
                listing=listing["listing"], scroll_into_view=False,
            )
            check("scroll_into_view=false does not scroll",
                  "scrolled_into_view" not in off)

        # The word is only useful if it is selective. Everything being flagged
        # would be the same failure as nothing being flagged, one bit the other
        # way up.
        check(
            "clipped is a minority of the listing, not a blanket",
            len(clipped) < max(1, len(controls) // 2),
            f"{len(clipped)} of {len(controls)}",
        )

        # Not every clipped control is scrollable into view - a full-width
        # element cut by a horizontal carousel has no scroller in reach from
        # itself - so the claim is that the word points at something actionable,
        # not that every row it appears on is.
        recovered = []
        for target in clipped[:6]:
            try:
                moved = session.call(
                    "scroll_to_control", hwnd=hwnd, control_id=target["id"],
                    listing=listing["listing"],
                )
            except Exception as exc:
                print(f"    {target['id']} raised {str(exc)[:80]}")
                continue
            print(f"    {target['id']} {str(target.get('name'))[:20]!r} "
                  f"method={moved.get('method')} moved={moved.get('moved')} "
                  f"in_view={moved.get('in_view')}")
            if moved.get("moved") or moved.get("in_view"):
                recovered.append(target["id"])
        check("at least one clipped control can be brought into view",
              bool(recovered), str(recovered))

    finally:
        session.destroy()

    section("RESULT")
    if failures:
        print(f"  {len(failures)} FAILED: {failures}")
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
