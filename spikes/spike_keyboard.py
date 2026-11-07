"""Is there ANY working keyboard path on a background desktop?

SendInput is out (no input queue) and PostMessage to the top-level WinUI 3 window
did nothing. This checks the remaining candidates against the Gallery's search
box, where a successful keystroke is immediately visible in the UI.

    python spikes/spike_keyboard.py
"""

from __future__ import annotations

import base64
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isodesk import SessionManager  # noqa: E402

AUMID = "Microsoft.WinUI3ControlsGallery_8wekyb3d8bbwe!App"


def main() -> int:
    from PIL import Image, ImageChops

    manager = SessionManager()
    session = manager.create(label="kbd-probe")
    findings = []

    try:
        session.call("activate", aumid=AUMID, wait=15)
        windows = session.call("list_windows")["windows"]
        gallery = next(w for w in windows if "WinUI" in w["class"])
        hwnd = gallery["hwnd"]

        def snap():
            payload = session.call("screenshot", hwnd=hwnd, max_width=None)
            return Image.open(io.BytesIO(base64.b64decode(payload["image_base64"])))

        def diff(a, b) -> float:
            if a.size != b.size:
                return 1.0
            d = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
            px = sum(1 for p in d.convert("L").resize((200, 200)).getdata() if p > 12)
            return round(px / 40000, 4)

        def attempt(name, fn, settle=1.5):
            before = snap()
            err = ""
            try:
                fn()
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
            time.sleep(settle)
            ratio = diff(before, snap())
            findings.append({"mechanism": name, "changed": ratio,
                             "worked": ratio > 0.002 and not err, "error": err[:70]})
            print(f"  {name:<34} worked={ratio > 0.002 and not err!s:<6} diff={ratio:<8} {err[:50]}")

        print("\nchild HWNDs of the WinUI 3 window:")
        children = session.call("_child_windows", hwnd=hwnd)["children"]
        for c in children:
            print(f"  hwnd={c['hwnd']} class={c['class']!r}")

        # Find the search box and give it UIA focus first - several mechanisms
        # only make sense against a focused edit control.
        controls = session.call("get_controls", hwnd=hwnd)["controls"]
        edit = next((c for c in controls if c["control_type"] == "Edit"), None)
        print(f"\nsearch box: {edit['id'] if edit else None}")

        print("\nmechanisms:")
        if edit:
            attempt("UIA ValuePattern (set_text)",
                    lambda: session.call("set_text", control_id=edit["id"],
                                         hwnd=hwnd, text="valuepattern"))

        for child in children:
            attempt(f"PostMessage WM_CHAR -> {child['class'][:22]}",
                    lambda c=child: session.call("_postmessage_chars",
                                                 hwnd=c["hwnd"], text="xyz"))

        attempt("PostMessage WM_CHAR -> top level",
                lambda: session.call("_postmessage_chars", hwnd=hwnd, text="xyz"))

        try:
            print(f"\nUIA focused element: {session.call('_focused_element')}")
        except Exception as exc:
            print(f"\nUIA focused element failed: {exc}")

        print("\n" + "=" * 70)
        working = [f for f in findings if f["worked"]]
        print(f"{len(working)} of {len(findings)} mechanisms produced a visible change")
        for f in working:
            print(f"  WORKS: {f['mechanism']}")
        return 0
    finally:
        manager.destroy(session.id)


if __name__ == "__main__":
    sys.exit(main())
