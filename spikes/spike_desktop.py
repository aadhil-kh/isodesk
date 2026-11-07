"""
Phase 0 feasibility spike for isodesk.

Question this answers: can a Win32 alternate desktop object host a real
(especially WinUI 3) application such that we can observe it and drive it,
*without* touching the primary desktop's foreground window, focus or cursor?

Run as:
    python spikes/spike_desktop.py                # host mode, drives everything
    python spikes/spike_desktop.py --probe FILE   # worker mode, runs on the desktop

The host creates the desktop, launches targets onto it, then spawns this same
script in --probe mode *onto that desktop* so every probe runs with the desktop
natively attached (no SetThreadDesktop gymnastics before COM init).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
import traceback
from ctypes import wintypes
from typing import Any, Callable, Dict, List, Optional

DESKTOP_NAME = "isodesk-spike"
WINSTA_DESKTOP = f"WinSta0\\{DESKTOP_NAME}"
GENERIC_ALL = 0x10000000

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.CreateDesktopW.restype = wintypes.HANDLE
user32.CreateDesktopW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
]
user32.OpenDesktopW.restype = wintypes.HANDLE
user32.OpenDesktopW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.BOOL,
    wintypes.DWORD,
]
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
user32.GetThreadDesktop.restype = wintypes.HANDLE
user32.GetThreadDesktop.argtypes = [wintypes.DWORD]

ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
)
user32.EnumDesktopWindows.argtypes = [
    wintypes.HANDLE,
    ENUM_WINDOWS_PROC,
    wintypes.LPARAM,
]


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------


def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def window_rect(hwnd: int) -> Dict[str, int]:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return {"left": r.left, "top": r.top, "right": r.right, "bottom": r.bottom}


def enum_desktop_windows(hdesk: int) -> List[Dict[str, Any]]:
    """Enumerate top-level windows belonging to a desktop handle."""
    found: List[Dict[str, Any]] = []

    def _cb(hwnd, _lparam):
        try:
            if user32.IsWindowVisible(hwnd):
                found.append(
                    {
                        "hwnd": hwnd,
                        "title": window_title(hwnd),
                        "class": window_class(hwnd),
                        "pid": window_pid(hwnd),
                        "rect": window_rect(hwnd),
                    }
                )
        except Exception:
            pass
        return True

    user32.EnumDesktopWindows(hdesk, ENUM_WINDOWS_PROC(_cb), 0)
    return found


def cursor_pos() -> Dict[str, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return {"x": pt.x, "y": pt.y}


def probe(results: Dict[str, Any], name: str, fn: Callable[[], Any]) -> Any:
    """Run one probe, recording pass/fail plus the returned detail."""
    entry: Dict[str, Any] = {"name": name}
    started = time.time()
    try:
        detail = fn()
        entry["ok"] = True
        entry["detail"] = detail
    except Exception as exc:
        entry["ok"] = False
        entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["traceback"] = traceback.format_exc(limit=4)
    entry["seconds"] = round(time.time() - started, 2)
    results["probes"].append(entry)
    return entry


# --------------------------------------------------------------------------
# worker mode: runs ON the isolated desktop
# --------------------------------------------------------------------------


def image_stats(img) -> Dict[str, Any]:
    """Quantify whether a captured image actually contains rendered content."""
    from PIL import Image  # noqa: F401

    rgb = img.convert("RGB")
    w, h = rgb.size
    pixels = list(rgb.getdata())
    total = len(pixels)
    non_black = sum(1 for p in pixels if p != (0, 0, 0))
    colors = rgb.getcolors(maxcolors=100000)
    return {
        "size": [w, h],
        "non_black_ratio": round(non_black / total, 4) if total else 0.0,
        "distinct_colors": len(colors) if colors else ">100000",
        "verdict": "CONTENT" if (non_black / total > 0.05 and colors and len(colors) > 4) else "BLANK",
    }


def capture_print_window(hwnd: int, flags: int, out_path: str) -> Dict[str, Any]:
    """PrintWindow capture. flags=2 is PW_RENDERFULLCONTENT."""
    import win32gui
    import win32ui
    from PIL import Image

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        raise RuntimeError(f"bad window rect {w}x{h}")

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bitmap)
    try:
        result = user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), flags)
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1
        )
        img.save(out_path)
        stats = image_stats(img)
        stats["printwindow_returned"] = result
        stats["saved"] = out_path
        return stats
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def capture_bitblt(hwnd: int, out_path: str) -> Dict[str, Any]:
    """BitBlt from the window DC - the classic path, expected to fail off-DWM."""
    import win32con
    import win32gui
    import win32ui
    from PIL import Image

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bitmap)
    try:
        save_dc.BitBlt((0, 0), (w, h), mfc_dc, (0, 0), win32con.SRCCOPY)
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1
        )
        img.save(out_path)
        stats = image_stats(img)
        stats["saved"] = out_path
        return stats
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def activate_packaged_app(aumid: str) -> Dict[str, Any]:
    """Activate an MSIX app via IApplicationActivationManager.

    Store-installed packages cannot be CreateProcess'd (their exe is ACL'd) and
    shell activation hands off to the running explorer on the Default desktop.
    Calling the activation manager from a process that is ALREADY on the
    isolated desktop is the remaining candidate for placing the app there.
    """
    import comtypes
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
    from ctypes import POINTER, c_uint, c_wchar_p

    class IApplicationActivationManager(IUnknown):
        _iid_ = GUID("{2e941141-7f97-4756-ba1d-9decde894a3d}")
        _methods_ = [
            COMMETHOD(
                [], HRESULT, "ActivateApplication",
                (["in"], c_wchar_p, "appUserModelId"),
                (["in"], c_wchar_p, "arguments"),
                (["in"], c_uint, "options"),
                (["out"], POINTER(wintypes.DWORD), "processId"),
            ),
        ]

    comtypes.CoInitialize()
    manager = comtypes.CoCreateInstance(
        GUID("{45BA127D-10A8-46EA-8AB7-56EA9078943C}"),
        interface=IApplicationActivationManager,
        clsctx=comtypes.CLSCTX_LOCAL_SERVER,
    )
    pid = manager.ActivateApplication(aumid, None, 0)
    return {"aumid": aumid, "pid": pid}


def run_worker(out_path: str, artifacts_dir: str) -> None:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware

    results: Dict[str, Any] = {"mode": "worker", "probes": []}

    # P0: activate a packaged app from INSIDE the isolated desktop, if asked.
    aumid = os.environ.get("ISODESK_ACTIVATE_AUMID")
    if aumid:
        probe(results, "P0 ActivateApplication from isolated desktop",
              lambda: activate_packaged_app(aumid))
        time.sleep(10)

    results["desktop_of_thread"] = user32.GetThreadDesktop(
        kernel32.GetCurrentThreadId()
    )
    results["pid"] = os.getpid()

    # ---- P1: do the launched apps actually live on this desktop? ----------
    p1 = probe(
        results,
        "P1 EnumDesktopWindows (own desktop)",
        lambda: enum_desktop_windows(
            user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
        ),
    )
    windows: List[Dict[str, Any]] = p1.get("detail") or []

    # Pick the most interesting window to probe against: prefer WinUI, then notepad.
    def pick(pred) -> Optional[Dict[str, Any]]:
        for w in windows:
            if pred(w):
                return w
        return None

    winui = pick(lambda w: "WinUI" in w["class"] or "Microsoft.UI" in w["class"])
    notepad = pick(lambda w: w["class"] in ("Notepad", "ApplicationFrameWindow")
                   or "Notepad" in w["title"])
    target = winui or notepad or (windows[0] if windows else None)
    results["target_window"] = target
    results["winui_window_present"] = winui is not None

    # ---- P2: UIA tree visibility ----------------------------------------
    def _p2():
        from pywinauto import Desktop

        desk = Desktop(backend="uia")
        tops = desk.windows()
        out = []
        for w in tops[:20]:
            try:
                out.append(
                    {
                        "title": w.window_text(),
                        "class": w.class_name(),
                        "control_type": getattr(w.element_info, "control_type", None),
                    }
                )
            except Exception as exc:
                out.append({"error": str(exc)})
        return {"top_level_count": len(tops), "sample": out}

    probe(results, "P2 UIA top-level enumeration", _p2)

    # ---- P2b: descend into the target window's control tree --------------
    def _p2b():
        if not target:
            raise RuntimeError("no target window on this desktop")
        from pywinauto import Desktop

        desk = Desktop(backend="uia")
        win = desk.window(handle=target["hwnd"])
        descendants = win.descendants()
        named = [
            {
                "name": d.window_text(),
                "type": getattr(d.element_info, "control_type", None),
                "auto_id": getattr(d.element_info, "automation_id", None),
                "rect": str(d.rectangle()),
            }
            for d in descendants[:40]
            if d.window_text()
        ]
        return {"descendant_count": len(descendants), "named_sample": named[:25]}

    probe(results, "P2b UIA control tree of target", _p2b)

    # ---- P3 / P4: pixel capture -----------------------------------------
    if target:
        probe(
            results,
            "P3 PrintWindow PW_RENDERFULLCONTENT",
            lambda: capture_print_window(
                target["hwnd"], 2, os.path.join(artifacts_dir, "p3_printwindow_full.png")
            ),
        )
        probe(
            results,
            "P3b PrintWindow flags=0",
            lambda: capture_print_window(
                target["hwnd"], 0, os.path.join(artifacts_dir, "p3b_printwindow_0.png")
            ),
        )
        probe(
            results,
            "P4 BitBlt window DC",
            lambda: capture_bitblt(
                target["hwnd"], os.path.join(artifacts_dir, "p4_bitblt.png")
            ),
        )

    # ---- P5: Windows.Graphics.Capture (optional dep) ---------------------
    def _p5():
        try:
            import winsdk  # noqa: F401
        except ImportError:
            raise RuntimeError("winsdk not installed - skipped, retry if P3 fails")
        raise RuntimeError("WGC probe not implemented yet; only run if P3/P4 blank")

    probe(results, "P5 Windows.Graphics.Capture", _p5)

    # ---- P6: does a UIA pattern actually drive the app? ------------------
    def _p6():
        if not target:
            raise RuntimeError("no target window")
        from pywinauto import Desktop

        desk = Desktop(backend="uia")
        win = desk.window(handle=target["hwnd"])
        # Exclude window caption buttons - invoking Minimize hides the window and
        # poisons every capture probe that runs after this one.
        chrome = {"Minimize", "Maximize", "Close", "Restore", "Back", "Settings"}
        buttons = [
            d
            for d in win.descendants()
            if getattr(d.element_info, "control_type", None) in ("Button", "MenuItem", "TabItem", "ListItem")
            and d.window_text()
            and d.window_text() not in chrome
        ]
        if not buttons:
            raise RuntimeError("no invokable control found")
        before = [w["title"] for w in enum_desktop_windows(
            user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
        )]
        chosen = buttons[0]
        label = chosen.window_text()
        # Try UIA patterns in reliability order. None of these need focus or
        # the input desktop, which is exactly why they are the primary path.
        errors = []
        method = None
        for name, action in (
            ("InvokePattern", lambda: chosen.invoke()),
            ("SelectionItemPattern", lambda: chosen.select()),
            ("ExpandCollapsePattern", lambda: chosen.expand()),
            ("LegacyIAccessible.DoDefaultAction",
             lambda: chosen.iface_legacy_iaccessible.DoDefaultAction()),
        ):
            try:
                action()
                method = name
                break
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}")
        if method is None:
            raise RuntimeError("all UIA action patterns failed: " + "; ".join(errors))
        time.sleep(1.0)
        after = [w["title"] for w in enum_desktop_windows(
            user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
        )]
        return {
            "control": label,
            "method": method,
            "pattern_errors": errors,
            "candidates": len(buttons),
            "titles_before": before,
            "titles_after": after,
            "state_changed": before != after,
        }

    probe(results, "P6 UIA invoke on target", _p6)

    # ---- P7: SendInput scoping ------------------------------------------
    def _p7():
        before = cursor_pos()
        if not target:
            raise RuntimeError("no target window")
        r = target["rect"]
        tx = (r["left"] + r["right"]) // 2
        ty = (r["top"] + r["bottom"]) // 2
        moved = user32.SetCursorPos(tx, ty)
        time.sleep(0.3)
        after = user32.GetCursorPos and cursor_pos()
        return {
            "setcursorpos_returned": bool(moved),
            "worker_cursor_before": before,
            "worker_cursor_after": after,
            "requested": {"x": tx, "y": ty},
        }

    probe(results, "P7 SetCursorPos on isolated desktop", _p7)

    # ---- P8: does a UIA action VISIBLY drive the app? --------------------
    # The real proof for the input layer: navigate the app via UIA, re-capture,
    # and require the pixels to actually differ.
    def _p8():
        if not target:
            raise RuntimeError("no target window")
        from pywinauto import Desktop

        desk = Desktop(backend="uia")
        win = desk.window(handle=target["hwnd"])

        # A minimized window renders nothing; restore before capturing.
        if user32.IsIconic(target["hwnd"]):
            user32.ShowWindow(target["hwnd"], 9)  # SW_RESTORE
            time.sleep(1.5)

        before_path = os.path.join(artifacts_dir, "p8_before.png")
        after_path = os.path.join(artifacts_dir, "p8_after.png")
        before_stats = capture_print_window(target["hwnd"], 2, before_path)

        # Prefer a navigation item that clearly repaints the content area.
        wanted = ("Design", "Accessibility", "Fundamentals", "All", "Settings")
        candidates = [
            d
            for d in win.descendants()
            if d.window_text() in wanted
            and getattr(d.element_info, "control_type", None)
            in ("ListItem", "TreeItem", "Button", "TabItem")
        ]
        if not candidates:
            raise RuntimeError("no navigation item found")
        chosen = candidates[0]
        label = chosen.window_text()

        errors = []
        method = None
        for name, action in (
            ("SelectionItemPattern", lambda: chosen.select()),
            ("InvokePattern", lambda: chosen.invoke()),
            ("LegacyIAccessible.DoDefaultAction",
             lambda: chosen.iface_legacy_iaccessible.DoDefaultAction()),
        ):
            try:
                action()
                method = name
                break
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}")
        if method is None:
            raise RuntimeError("all nav patterns failed: " + "; ".join(errors))

        time.sleep(2.0)
        after_stats = capture_print_window(target["hwnd"], 2, after_path)

        from PIL import Image, ImageChops

        diff = ImageChops.difference(
            Image.open(before_path).convert("RGB"),
            Image.open(after_path).convert("RGB"),
        )
        bbox = diff.getbbox()
        changed_px = sum(1 for p in diff.convert("L").getdata() if p > 12)
        total_px = diff.size[0] * diff.size[1]

        return {
            "navigated_to": label,
            "method": method,
            "pattern_errors": errors,
            "changed_pixel_ratio": round(changed_px / total_px, 4),
            "diff_bbox": bbox,
            "ui_actually_changed": changed_px / total_px > 0.01,
            "before": before_stats["verdict"],
            "after": after_stats["verdict"],
        }

    probe(results, "P8 UIA navigation drives the app (pixel diff)", _p8)

    results["foreground_window_on_this_desktop"] = {
        "hwnd": user32.GetForegroundWindow(),
        "title": window_title(user32.GetForegroundWindow()),
    }

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, default=str)


# --------------------------------------------------------------------------
# host mode
# --------------------------------------------------------------------------


def find_packaged_exe(package_prefix: str, exe_name: str) -> Optional[str]:
    """Locate a packaged app's exe under WindowsApps.

    Launching this path directly is a plain CreateProcess (so lpDesktop is
    honoured) while Windows still grants the process package identity.
    """
    root = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "WindowsApps")
    try:
        for name in os.listdir(root):
            if name.startswith(package_prefix):
                candidate = os.path.join(root, name, exe_name)
                if os.path.exists(candidate):
                    return candidate
    except Exception as exc:
        print(f"[host] WindowsApps scan failed: {exc}")
    return None


def launch_on_desktop(cmdline: str, desktop: str) -> Dict[str, Any]:
    """CreateProcess with STARTUPINFO.lpDesktop pointing at the isolated desktop."""
    import win32process

    si = win32process.STARTUPINFO()
    si.lpDesktop = desktop
    try:
        hproc, hthread, pid, tid = win32process.CreateProcess(
            None, cmdline, None, None, False, 0, None, None, si
        )
        return {"cmdline": cmdline, "pid": pid, "ok": True}
    except Exception as exc:
        return {"cmdline": cmdline, "ok": False, "error": str(exc)}


def run_host(targets: List[str]) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    artifacts = os.path.join(here, "artifacts")
    os.makedirs(artifacts, exist_ok=True)
    results_path = os.path.join(artifacts, "probe_results.json")
    if os.path.exists(results_path):
        os.remove(results_path)

    host_state_before = {
        "cursor": cursor_pos(),
        "foreground_hwnd": user32.GetForegroundWindow(),
        "foreground_title": window_title(user32.GetForegroundWindow()),
        "window_count": len(
            enum_desktop_windows(user32.GetThreadDesktop(kernel32.GetCurrentThreadId()))
        ),
    }
    print(f"[host] before: {json.dumps(host_state_before)}")

    hdesk = user32.CreateDesktopW(DESKTOP_NAME, None, None, 0, GENERIC_ALL, None)
    if not hdesk:
        err = ctypes.get_last_error()
        print(f"[host] FATAL CreateDesktopW failed, GetLastError={err}")
        return 1
    print(f"[host] created desktop '{DESKTOP_NAME}' hdesk={hdesk}")

    launched = []
    try:
        if "notepad" in targets:
            # GDI baseline: isolates "desktop is broken" from "WinUI 3 is broken"
            launched.append(launch_on_desktop("notepad.exe", WINSTA_DESKTOP))
        if "gallery" in targets:
            # Packaged WinUI 3 app launched by its exe directly out of
            # WindowsApps. This keeps package identity but is a plain
            # CreateProcess, so STARTUPINFO.lpDesktop is honoured.
            exe = find_packaged_exe(
                "Microsoft.WinUI3ControlsGallery", "WinUIGallery.exe"
            )
            if exe:
                launched.append(launch_on_desktop(f'"{exe}"', WINSTA_DESKTOP))
            else:
                print("[host] gallery exe not found under WindowsApps")
        if "gallery-shell" in targets:
            # Kept only to document the negative result: shell activation hands
            # off to the RUNNING explorer, so the app lands on the host's
            # Default desktop and steals focus. Never use this path.
            launched.append(
                launch_on_desktop(
                    "explorer.exe shell:AppsFolder\\"
                    "Microsoft.WinUI3ControlsGallery_8wekyb3d8bbwe!App",
                    WINSTA_DESKTOP,
                )
            )
        if "winui" in targets:
            exe = os.environ.get("ISODESK_WINUI_EXE")
            if exe:
                launched.append(launch_on_desktop(f'"{exe}"', WINSTA_DESKTOP))
            else:
                print("[host] skipping 'winui' target: set ISODESK_WINUI_EXE first")
        for entry in launched:
            print(f"[host] launch {entry}")

        print("[host] waiting 12s for apps to start...")
        time.sleep(12)

        print("[host] spawning probe worker onto the isolated desktop")
        worker = launch_on_desktop(
            f'"{sys.executable}" "{os.path.abspath(__file__)}" '
            f'--probe "{results_path}" --artifacts "{artifacts}"',
            WINSTA_DESKTOP,
        )
        print(f"[host] worker {worker}")

        deadline = time.time() + 120
        while time.time() < deadline and not os.path.exists(results_path):
            time.sleep(1)

        host_state_after = {
            "cursor": cursor_pos(),
            "foreground_hwnd": user32.GetForegroundWindow(),
            "foreground_title": window_title(user32.GetForegroundWindow()),
            "window_count": len(
                enum_desktop_windows(
                    user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
                )
            ),
        }

        print("\n" + "=" * 70)
        print("HOST ISOLATION CHECK (primary desktop must be untouched)")
        print("=" * 70)
        print(f"  cursor      before={host_state_before['cursor']} after={host_state_after['cursor']}"
              f"  -> {'OK' if host_state_before['cursor'] == host_state_after['cursor'] else 'VIOLATED'}")
        print(f"  foreground  before={host_state_before['foreground_title']!r} "
              f"after={host_state_after['foreground_title']!r}"
              f"  -> {'OK' if host_state_before['foreground_hwnd'] == host_state_after['foreground_hwnd'] else 'VIOLATED'}")
        print(f"  windows     before={host_state_before['window_count']} "
              f"after={host_state_after['window_count']}"
              f"  -> {'OK' if host_state_before['window_count'] == host_state_after['window_count'] else 'CHANGED'}")

        if not os.path.exists(results_path):
            print("\n[host] FATAL: worker produced no results (it may have crashed).")
            return 2

        with open(results_path, encoding="utf-8") as fh:
            res = json.load(fh)

        print("\n" + "=" * 70)
        print("PROBE MATRIX (inside the isolated desktop)")
        print("=" * 70)
        for p in res["probes"]:
            status = "PASS" if p.get("ok") else "FAIL"
            print(f"  [{status}] {p['name']}  ({p['seconds']}s)")
            if p.get("ok"):
                detail = p["detail"]
                if isinstance(detail, dict):
                    for k in ("verdict", "non_black_ratio", "distinct_colors",
                              "top_level_count", "descendant_count", "state_changed",
                              "control", "method", "printwindow_returned"):
                        if k in detail:
                            print(f"           {k}: {detail[k]}")
                elif isinstance(detail, list):
                    print(f"           {len(detail)} windows on desktop:")
                    for w in detail[:10]:
                        print(f"             - {w['class']!r} pid={w['pid']} {w['title']!r}")
            else:
                print(f"           {p.get('error')}")

        print(f"\n  winui_window_present: {res.get('winui_window_present')}")
        print(f"  target_window: {res.get('target_window')}")
        print(f"\n[host] full results: {results_path}")
        print(f"[host] captured images: {artifacts}")
        return 0

    finally:
        print("\n[host] cleaning up")
        try:
            # Safety: only kill a PID if it has windows on the isolated desktop and
            # NONE on the host's default desktop. A PID present on both is a host
            # process and must never be touched.
            host_desk = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
            host_pids = {w["pid"] for w in enum_desktop_windows(host_desk)}
            iso_windows = enum_desktop_windows(hdesk)
            iso_pids = {w["pid"] for w in iso_windows}
            killable = (iso_pids - host_pids) - {os.getpid(), 0}
            skipped = iso_pids & host_pids
            if skipped:
                print(f"[host] NOT killing PIDs also present on host desktop: {skipped}")
            for pid in killable:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True
                )
                print(f"[host] killed pid {pid}")
            if not killable:
                print("[host] nothing to kill on the isolated desktop")
        except Exception as exc:
            print(f"[host] cleanup failed: {exc}")
        user32.CloseDesktop(hdesk)
        print("[host] desktop handle closed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", help="worker mode: write results JSON here")
    parser.add_argument("--artifacts", help="worker mode: directory for captures")
    parser.add_argument(
        "--targets",
        default="notepad",
        help="comma-separated: notepad,gallery,winui (default: notepad)",
    )
    args = parser.parse_args()

    if args.probe:
        run_worker(args.probe, args.artifacts or os.path.dirname(args.probe))
        return 0
    return run_host([t.strip() for t in args.targets.split(",") if t.strip()])


if __name__ == "__main__":
    sys.exit(main())
