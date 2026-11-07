"""End-to-end smoke test: create a session, drive a WinUI 3 app, tear it down.

Run directly (not under pytest) so the output is readable while iterating:

    python tests/smoke_e2e.py
    python tests/smoke_e2e.py --aumid "Microsoft.WinUI3ControlsGallery_8wekyb3d8bbwe!App"
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins", "isodesk", "server"))

from isodesk import SessionManager  # noqa: E402
from isodesk.win32 import desktop as dt  # noqa: E402

ARTIFACTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")


def save(name: str, b64: str) -> str:
    os.makedirs(ARTIFACTS, exist_ok=True)
    path = os.path.join(ARTIFACTS, name)
    with open(path, "wb") as fh:
        fh.write(base64.b64decode(b64))
    return path


def step(label: str):
    print(f"\n--- {label} ---")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aumid", default="Microsoft.WinUI3ControlsGallery_8wekyb3d8bbwe!App")
    parser.add_argument("--keep", action="store_true", help="do not destroy the session")
    args = parser.parse_args()

    host_fg_before = dt.foreground_window()
    host_windows_before = len(dt.app_windows(dt.current_thread_desktop()))
    print(f"[host] foreground={host_fg_before} windows={host_windows_before}")

    manager = SessionManager()
    session = None
    try:
        step("create session")
        started = time.time()
        session = manager.create(label="smoke")
        print(f"session {session.id} up in {time.time() - started:.1f}s")
        print(f"ping -> {session.call('ping')}")

        step("launch notepad (unpackaged exe path)")
        print(session.call("launch", cmdline="notepad.exe", wait=8))

        step("activate WinUI 3 Gallery (packaged app path)")
        print(session.call("activate", aumid=args.aumid, wait=15))

        step("list windows")
        windows = session.call("list_windows")
        for w in windows["windows"]:
            print(f"  hwnd={w['hwnd']} class={w['class']!r} title={w['title']!r}")

        gallery = next(
            (w for w in windows["windows"] if "WinUI" in w["class"]), None
        )
        if not gallery:
            print("!! Gallery window not found; aborting UI steps")
            return 1
        hwnd = gallery["hwnd"]

        step("screenshot")
        shot = session.call("screenshot", hwnd=hwnd)
        print(f"  quality={shot['quality']}")
        print(f"  saved {save('e2e_plain.png', shot['image_base64'])}")
        if not shot["quality"]["looks_rendered"]:
            print("!! capture looks blank")
            return 1

        step("get controls")
        controls = session.call("get_controls", hwnd=hwnd)
        print(f"  {controls['count']} actionable controls")
        for c in controls["controls"][:12]:
            print(f"    {c['id']:>4}  {c['control_type']:<12} {c['name']!r}")

        step("annotated screenshot (set-of-mark)")
        annotated = session.call("screenshot", hwnd=hwnd, annotated=True, max_width=1400)
        print(f"  saved {save('e2e_annotated.png', annotated['image_base64'])}")

        step("drive the app via UIA")
        nav = next(
            (c for c in controls["controls"]
             if c["name"] in ("Design", "Accessibility", "Fundamentals", "All")),
            None,
        )
        if nav:
            print(f"  clicking {nav['name']!r} ({nav['id']})")
            print(f"  -> {session.call('click_control', control_id=nav['id'], hwnd=hwnd)}")
            time.sleep(2)
            after = session.call("screenshot", hwnd=hwnd)
            print(f"  saved {save('e2e_after_click.png', after['image_base64'])}")
        else:
            print("  no navigation control found; skipped")

        step("type into the search box via ValuePattern")
        search = next(
            (c for c in controls["controls"]
             if c["control_type"] == "Edit"
             or "Search" in (c["name"] or "")
             or "Search" in (c["automation_id"] or "")),
            None,
        )
        if search:
            print(f"  setting text on {search['name']!r} ({search['id']})")
            print(f"  -> {session.call('set_text', control_id=search['id'], hwnd=hwnd, text='button')}")
            time.sleep(1.5)
            typed = session.call("screenshot", hwnd=hwnd)
            print(f"  saved {save('e2e_after_type.png', typed['image_base64'])}")
        else:
            print("  no edit control found; skipped")

        step("host isolation check")
        host_fg_after = dt.foreground_window()
        host_windows_after = len(dt.app_windows(dt.current_thread_desktop()))
        print(f"  foreground unchanged: {host_fg_before == host_fg_after}")
        print(f"  host window count: {host_windows_before} -> {host_windows_after}")
        if host_windows_after != host_windows_before:
            print("  !! a window leaked onto the host desktop")
            return 1

        return 0

    finally:
        if session and not args.keep:
            step("destroy session")
            print(manager.destroy(session.id))
        elif session:
            print(f"\n[kept] session {session.id} still running")


if __name__ == "__main__":
    sys.exit(main())
