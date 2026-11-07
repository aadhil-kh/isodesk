"""Verify the popup / scroll / caption-classification cut against a real WinUI 3 app.

Covers the four fixes that came out of driving `fluent_devutils` through isodesk:

  * popups reported by screenshot - a flyout or drop-down is its own top-level
    window, so it is absent from the parent's capture
  * a popup's items are enumerable and actionable by its own hwnd
  * the ComboBox light-dismiss overlay is named "Close" but is NOT a caption button
  * scroll_control resolves to the nearest scrollable instead of failing on the
    container it was handed
  * capturing a dead hwnd says the window is gone, not "empty rect (0x0)"

    python tests/smoke_popups_scroll.py
    python tests/smoke_popups_scroll.py --aumid "..."
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins", "isodesk", "server"))

from isodesk import SessionManager  # noqa: E402

DEFAULT_AUMID = "4315AadhilRF.DevUtils-JSONFormatterJSONtoXMLandQRG_hqkbg2ta66qf0!App"


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aumid", default=DEFAULT_AUMID)
    args = parser.parse_args()

    manager = SessionManager()
    session = manager.create(label="popups")
    failures = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' :: ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    try:
        session.call("activate", aumid=args.aumid, wait=20)
        hwnd = next(w for w in session.call("list_windows")["windows"]
                    if "WinUI" in w["class"])["hwnd"]

        # Settings is always present in the nav pane and holds a ComboBox, which
        # is the cheapest reliable popup to open on any app of this shape.
        controls = session.call("get_controls", hwnd=hwnd)["controls"]
        settings = next(c for c in controls if c["automation_id"] == "SettingsItem")
        session.call("select_control", hwnd=hwnd, control_id=settings["id"])
        time.sleep(1.0)

        section("scroll_control resolves to the nearest scrollable")
        controls = session.call("get_controls", hwnd=hwnd)["controls"]
        nav = next(c for c in controls if c["automation_id"] == "RootNavigationView")
        try:
            scrolled = session.call("scroll_control", hwnd=hwnd,
                                    control_id=nav["id"], direction="down", amount=3)
            print(f"  method={scrolled.get('method')} scrolled={scrolled.get('scrolled')}")
            if scrolled.get("scrolled_element"):
                print(f"  actually scrolled: {scrolled['scrolled_element']}")
            check("NavigationView scroll succeeds", scrolled.get("method") == "ScrollPattern")
            check("reports which element moved", scrolled.get("scrolled") in
                  ("self", "descendant", "ancestor"))
        except Exception as exc:
            # A container with nothing scrollable inside is a legitimate outcome;
            # what must not happen is the old GetLastError=5 wheel-input noise.
            print(f"  raised: {exc}")
            check("no misleading wheel/SendInput error", "SendInput" not in str(exc))

        section("popup reported by screenshot, absent from its image")
        controls = session.call("get_controls", hwnd=hwnd)["controls"]
        combo = next(c for c in controls if c["control_type"] == "ComboBox")
        session.call("expand_control", hwnd=hwnd, control_id=combo["id"])
        time.sleep(0.8)

        shot = session.call("screenshot", hwnd=hwnd, max_width=600)
        popups = shot.get("popups") or []
        print(f"  popups reported: {[(p['hwnd'], p['rect']['width'], p['rect']['height']) for p in popups]}")
        check("screenshot reports the open popup", len(popups) >= 1)

        windows = session.call("list_windows")["windows"]
        flagged = [w for w in windows if w.get("popup")]
        print(f"  list_windows popup flags: {[(w['hwnd'], w['class']) for w in flagged]}")
        check("list_windows flags the popup", len(flagged) >= 1)

        if popups:
            section("the popup is capturable and actionable by its own hwnd")
            popup_hwnd = popups[0]["hwnd"]
            popup_shot = session.call("screenshot", hwnd=popup_hwnd, max_width=400)
            check("popup captures a rendered frame",
                  popup_shot["quality"]["looks_rendered"],
                  str(popup_shot["quality"]))

            popup_controls = session.call("get_controls", hwnd=popup_hwnd)["controls"]
            items = [c for c in popup_controls if c["xaml_type"] == "ComboBoxItem"]
            print(f"  drop-down items: {[c['name'] for c in items]}")
            check("popup items enumerated", len(items) >= 2)

            section("light-dismiss overlay is not mistaken for a caption button")
            dismiss = [c for c in popup_controls if c["xaml_type"] == "ComboBoxLightDismiss"]
            print(f"  light dismiss entries: "
                  f"{[(c['name'], c.get('caption_button', False)) for c in dismiss]}")
            check("light dismiss found", len(dismiss) >= 1)
            check("light dismiss NOT flagged as caption button",
                  all(not c.get("caption_button") for c in dismiss))

            frame = [c for c in controls if c["name"] in ("Close", "Minimize", "Maximize")
                     and not c["class_name"]]
            check("real frame buttons still flagged",
                  bool(frame) and all(c.get("caption_button") for c in frame),
                  f"{[(c['name'], c.get('caption_button')) for c in frame]}")

        section("a dead hwnd reports the window is gone")
        try:
            session.call("screenshot", hwnd=0x7FFFFFF0, max_width=200)
            check("dead hwnd raises", False, "no error raised")
        except Exception as exc:
            print(f"  raised: {exc}")
            check("names the real cause", "no longer exists" in str(exc))

    finally:
        manager.destroy(session.id)

    section("RESULT")
    if failures:
        print(f"  {len(failures)} failure(s): {failures}")
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
