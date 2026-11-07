"""Win32 desktop-object primitives.

A desktop object is the isolation boundary isodesk is built on: it owns its own
foreground window, focus, z-order and cursor. Windows on a non-input desktop are
invisible to the user and cannot steal their focus, yet - as the Phase 0 spike
established - they are still composited well enough for PrintWindow to capture
them, and UIA sees them normally.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any, Callable, Dict, List, Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_ALL = 0x10000000
SW_RESTORE = 9

# Access rights needed to open an existing desktop and enumerate/act on it.
DESKTOP_READOBJECTS = 0x0001
DESKTOP_CREATEWINDOW = 0x0002
DESKTOP_CREATEMENU = 0x0004
DESKTOP_HOOKCONTROL = 0x0008
DESKTOP_JOURNALRECORD = 0x0010
DESKTOP_JOURNALPLAYBACK = 0x0020
DESKTOP_ENUMERATE = 0x0040
DESKTOP_WRITEOBJECTS = 0x0080
DESKTOP_SWITCHDESKTOP = 0x0100
DESKTOP_ALL = (
    DESKTOP_READOBJECTS
    | DESKTOP_CREATEWINDOW
    | DESKTOP_CREATEMENU
    | DESKTOP_HOOKCONTROL
    | DESKTOP_JOURNALRECORD
    | DESKTOP_JOURNALPLAYBACK
    | DESKTOP_ENUMERATE
    | DESKTOP_WRITEOBJECTS
    | DESKTOP_SWITCHDESKTOP
)

user32.CreateDesktopW.restype = wintypes.HANDLE
user32.CreateDesktopW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
]
user32.OpenDesktopW.restype = wintypes.HANDLE
user32.OpenDesktopW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
]
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
user32.CloseDesktop.restype = wintypes.BOOL
user32.GetThreadDesktop.restype = wintypes.HANDLE
user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
user32.SetThreadDesktop.restype = wintypes.BOOL
user32.SwitchDesktop.argtypes = [wintypes.HANDLE]
user32.SwitchDesktop.restype = wintypes.BOOL

_ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumDesktopWindows.argtypes = [wintypes.HANDLE, _ENUM_WINDOWS_PROC, wintypes.LPARAM]
user32.EnumDesktopWindows.restype = wintypes.BOOL

_ENUM_DESKTOPS_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.LPWSTR, wintypes.LPARAM)
user32.GetProcessWindowStation.restype = wintypes.HANDLE
user32.EnumDesktopsW.argtypes = [wintypes.HANDLE, _ENUM_DESKTOPS_PROC, wintypes.LPARAM]
user32.EnumDesktopsW.restype = wintypes.BOOL


class DesktopError(RuntimeError):
    """A desktop-object operation failed; carries GetLastError."""


def _check(handle_or_bool, what: str):
    if not handle_or_bool:
        raise DesktopError(f"{what} failed (GetLastError={ctypes.get_last_error()})")
    return handle_or_bool


def desktop_path(name: str) -> str:
    """The `WinSta0\\<name>` form required by STARTUPINFO.lpDesktop."""
    return f"WinSta0\\{name}"


def create_desktop(name: str) -> int:
    """Create a desktop object and return its handle.

    The handle must be held open for as long as the session lives: a desktop is
    destroyed once its last open handle is closed and no threads remain on it.
    """
    return _check(
        user32.CreateDesktopW(name, None, None, 0, GENERIC_ALL, None),
        f"CreateDesktopW({name!r})",
    )


def open_desktop(name: str, access: int = DESKTOP_ALL) -> int:
    return _check(user32.OpenDesktopW(name, 0, False, access), f"OpenDesktopW({name!r})")


def close_desktop(hdesk: int) -> None:
    user32.CloseDesktop(hdesk)


def current_thread_desktop() -> int:
    return user32.GetThreadDesktop(kernel32.GetCurrentThreadId())


def switch_desktop(hdesk: int) -> None:
    """Make a desktop the *input* desktop - it takes over the physical display.

    Only ever call this for an explicit, user-requested "show me the session"
    action. It is the one operation in isodesk that deliberately breaks isolation.
    """
    _check(user32.SwitchDesktop(hdesk), "SwitchDesktop")


# ---------------------------------------------------------------------------
# window helpers (usable against any desktop, given its handle)
# ---------------------------------------------------------------------------


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
    return {
        "left": r.left, "top": r.top, "right": r.right, "bottom": r.bottom,
        "width": r.right - r.left, "height": r.bottom - r.top,
    }


def dpi_for_window(hwnd: int) -> int:
    """Per-window DPI. 96 means 100% scaling."""
    try:
        user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        user32.GetDpiForWindow.restype = wintypes.UINT
        dpi = user32.GetDpiForWindow(hwnd)
        return dpi or 96
    except Exception:
        return 96  # pre-1607; assume 100%


def dpi_scale_for_window(hwnd: int) -> float:
    """Scale factor, e.g. 2.0 at 200%.

    Reported alongside every rect because XAML adaptive thresholds are in
    effective pixels: at 200% a 2560px window is 1280 DIPs, so a
    MinWindowWidth=1060 trigger is still in its wide state. Without the scale in
    the payload that has to be eyeballed from glyph size in a screenshot.
    """
    return dpi_for_window(hwnd) / 96.0


def to_dips(rect: Dict[str, int], scale: float) -> Dict[str, int]:
    return {k: round(v / scale) for k, v in rect.items()}


def is_minimized(hwnd: int) -> bool:
    return bool(user32.IsIconic(hwnd))


def restore_window(hwnd: int) -> None:
    """Un-minimize. A minimized window renders nothing, so capture returns blank."""
    user32.ShowWindow(hwnd, SW_RESTORE)


def move_window(hwnd: int, x: int, y: int, width: int, height: int) -> None:
    user32.MoveWindow(hwnd, x, y, width, height, True)


def enum_desktop_windows(
    hdesk: int, visible_only: bool = True, predicate: Optional[Callable] = None
) -> List[Dict[str, Any]]:
    """List top-level windows on a desktop.

    Note EnumDesktopWindows reports every top-level window including invisible
    helpers (UAC input indicators and the like), so callers that want "real"
    windows should filter on a non-empty title.
    """
    found: List[Dict[str, Any]] = []

    def _cb(hwnd, _lparam):
        try:
            if visible_only and not user32.IsWindowVisible(hwnd):
                return True
            info = {
                "hwnd": hwnd,
                "title": window_title(hwnd),
                "class": window_class(hwnd),
                "pid": window_pid(hwnd),
                "rect": window_rect(hwnd),
                "minimized": is_minimized(hwnd),
            }
            if predicate is None or predicate(info):
                found.append(info)
        except Exception:
            pass  # a window can die mid-enumeration; skip it
        return True

    user32.EnumDesktopWindows(hdesk, _ENUM_WINDOWS_PROC(_cb), 0)
    return found


# Many WinUI 3 flyouts, menus, ComboBox drop-downs and tooltips are hosted in
# their own top-level window of this class, titled "PopupHost", rather than
# inside the parent. That has a sharp consequence for capture - PrintWindow on
# the parent renders the parent alone, so such a drop-down is simply absent from
# the frame, and the control underneath it looks closed.
#
# It is not universal, and the exception matters because it fails in the
# convincing direction. A XAML `Flyout` is sometimes hosted inside the parent's
# own content, and then there is no window of this class at all: this detection
# reports nothing, `app_windows` shows only the main window, and both signals
# read "it never opened" about a flyout that did. The discriminator that works
# either way is the parent's UIA tree - an in-frame flyout's items are in it.
POPUP_WINDOW_CLASSES = {"Microsoft.UI.Content.PopupWindowSiteBridge"}


def is_popup_window(info: Dict[str, Any]) -> bool:
    return info.get("class") in POPUP_WINDOW_CLASSES or info.get("title") == "PopupHost"


def app_windows(hdesk: int) -> List[Dict[str, Any]]:
    """Top-level windows that a user would consider real application windows.

    Popup hosts are included - they are genuinely separate windows and the only
    way to see or act on a flyout - but flagged, so a caller can tell the app's
    window from the drop-down floating over it.
    """
    windows = enum_desktop_windows(
        hdesk,
        visible_only=True,
        predicate=lambda w: bool(w["title"]) and w["class"] != "UAC_InputIndicatorOverlayWnd",
    )
    for window in windows:
        if is_popup_window(window):
            window["popup"] = True
    return windows


def enum_desktops() -> List[str]:
    """Names of every desktop on this process's window station.

    This is how a leaked session is found at all. A desktop object outlives the
    process that created it for as long as anything is still running on it, so a
    host that dies without calling `destroy` leaves both the desktop and the app
    on it - invisible, holding the app's binaries open, and impossible to see
    from the session registry, which died with the host.
    """
    names: List[str] = []

    def _cb(name, _lparam):
        if name:
            names.append(name)
        return True

    user32.EnumDesktopsW(
        user32.GetProcessWindowStation(), _ENUM_DESKTOPS_PROC(_cb), 0
    )
    return names


def window_exists(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


def cursor_pos() -> Dict[str, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return {"x": pt.x, "y": pt.y}


def foreground_window() -> int:
    return user32.GetForegroundWindow()
