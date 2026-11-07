"""Which input mechanisms actually work on a non-input desktop?

`SendInput` and `SetCursorPos` fail there - hardware-level injection only works
on the desktop that owns the physical input queue. This probe measures what is
left, against both a Win32-shell app (Notepad) and a WinUI 3 app (Gallery),
verifying each mechanism by pixel diff rather than by return code.

    python spikes/spike_input.py
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isodesk import SessionManager  # noqa: E402
from isodesk.capture import capture_window  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)

ARTIFACTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts")

PROBE_SOURCE = r'''
import ctypes, json, time
from ctypes import wintypes
user32 = ctypes.WinDLL("user32", use_last_error=True)

WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
WM_MOUSEWHEEL = 0x020A
WM_SETTEXT = 0x000C

def make_lparam(x, y):
    return (y << 16) | (x & 0xFFFF)

def results():
    return RESULTS

RESULTS = []

def record(name, ok, detail=""):
    RESULTS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
'''


def _worker_probe_code() -> str:
    """Python source the worker runs via its `run` handler."""
    return PROBE_SOURCE


def probe(session, hwnd: int, rect: Dict[str, int], label: str) -> List[Dict[str, Any]]:
    """Run each input mechanism and verify it by pixel diff on the window.

    Captures go through the session RPC, never directly: window geometry and
    device contexts of another desktop are not queryable from the host process
    (GetWindowRect returns 0x0), so the worker must do the capturing.
    """
    import base64
    import io

    from PIL import Image, ImageChops

    out: List[Dict[str, Any]] = []
    cx = (rect["left"] + rect["right"]) // 2
    cy = (rect["top"] + rect["bottom"]) // 2

    def snap() -> Image.Image:
        payload = session.call("screenshot", hwnd=hwnd, max_width=None)
        return Image.open(io.BytesIO(base64.b64decode(payload["image_base64"])))

    def changed(before, after) -> float:
        if before.size != after.size:
            return 1.0
        diff = ImageChops.difference(before.convert("RGB"), after.convert("RGB"))
        px = sum(1 for p in diff.convert("L").resize((160, 160)).getdata() if p > 12)
        return round(px / (160 * 160), 4)

    def attempt(name: str, fn, settle: float = 1.2) -> None:
        before = snap()
        error = ""
        try:
            fn()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        time.sleep(settle)
        after = snap()
        ratio = changed(before, after)
        out.append({
            "target": label,
            "mechanism": name,
            "raised": error,
            "changed_ratio": ratio,
            "worked": ratio > 0.002 and not error,
        })

    # 1. UIA element-from-point, then invoke it. The coordinate-addressed action
    #    that does not need the input desktop.
    attempt("uia_elementfrompoint_invoke",
            lambda: session.call("click_xy", x=cx, y=cy))

    # 2. Raw SendInput click - expected to fail on a background desktop.
    attempt("sendinput_click",
            lambda: session.call("_raw_sendinput_click", x=cx, y=cy))

    # 3. PostMessage click to the window.
    attempt("postmessage_click",
            lambda: session.call("_postmessage_click", hwnd=hwnd,
                                 x=cx - rect["left"], y=cy - rect["top"]))

    # 4. PostMessage characters.
    attempt("postmessage_wm_char",
            lambda: session.call("_postmessage_chars", hwnd=hwnd, text="probe"))

    # 5. pywinauto type_keys (SendInput underneath) - expected to fail.
    attempt("type_keys_sendinput",
            lambda: session.call("send_keys", keys="probe", hwnd=hwnd))

    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aumid", default="Microsoft.WinUI3ControlsGallery_8wekyb3d8bbwe!App")
    args = parser.parse_args()

    os.makedirs(ARTIFACTS, exist_ok=True)
    manager = SessionManager()
    session = manager.create(label="input-probe")
    findings: List[Dict[str, Any]] = []

    try:
        session.call("launch", cmdline="notepad.exe", wait=8)
        session.call("activate", aumid=args.aumid, wait=15)
        windows = session.call("list_windows")["windows"]

        for w in windows:
            label = "WinUI3" if "WinUI" in w["class"] else w["class"]
            print(f"\n=== probing {label}: {w['title']!r} ===")
            try:
                findings.extend(probe(session, w["hwnd"], w["rect"], label))
            except Exception as exc:
                print(f"  probe failed: {exc}")

        print("\n" + "=" * 78)
        print(f"{'target':<10} {'mechanism':<32} {'worked':<8} {'diff':<8} error")
        print("=" * 78)
        for f in findings:
            print(f"{f['target']:<10} {f['mechanism']:<32} "
                  f"{str(f['worked']):<8} {f['changed_ratio']:<8} {f['raised'][:40]}")

        path = os.path.join(ARTIFACTS, "input_probe.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(findings, fh, indent=2)
        print(f"\nsaved {path}")
        return 0
    finally:
        manager.destroy(session.id)


if __name__ == "__main__":
    sys.exit(main())
