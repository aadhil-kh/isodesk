"""Input actions scoped to the isolated desktop.

The usual Python automation stack - `set_focus()`, pywinauto `click_input()`,
`pyautogui` - drives the real cursor on the input desktop, which is exactly the
host coupling isodesk exists to remove, so none of it is used here.

Order of preference, established in Phase 0:

  1. UIA patterns   - focus-free, compositing-free, worked on WinUI 3 first try
  2. SendInput      - desktop-scoped when called from the worker; needed for
                      anything without a usable pattern
  3. PostMessage    - deliberately absent. WinUI 3 routes pointer input through
                      the input-site bridge and ignores synthesized WM_* sent to
                      the top-level HWND, so it would fail silently.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any, Dict, List, Optional

from .uia import UiaError, get_window

user32 = ctypes.WinDLL("user32", use_last_error=True)

# Spelled out because WindowFromPoint takes a POINT *by value* and
# SendMessageTimeoutW writes through an out-pointer - neither survives ctypes'
# default int-sized argument handling on x64.
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetAncestor.restype = wintypes.HWND
user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
    ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t),
]
user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t


class ActionError(RuntimeError):
    pass


class Refused(ActionError):
    """A deliberate refusal, as opposed to "that did not work".

    The distinction exists because `click_xy` has a fallback ladder: an
    ActionError means "UIA could not do it, try PostMessage", but the caption
    guard means "do not do this at all", and falling back would post the click
    onto the Close button the guard just refused. Anything that raises this must
    escape the fallback.
    """


# ---------------------------------------------------------------------------
# UIA-pattern actions
# ---------------------------------------------------------------------------


def _try_patterns(element, candidates) -> Dict[str, Any]:
    """Attempt UIA patterns in order; report which one took effect."""
    errors: List[str] = []
    for name, action in candidates:
        try:
            action()
            return {"method": name, "attempts": errors}
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    raise ActionError("no UIA pattern succeeded -> " + " | ".join(errors))


def invoke_control(element) -> Dict[str, Any]:
    """Activate a control the way a click would, without a click."""
    return _try_patterns(
        element,
        (
            ("InvokePattern", lambda: element.invoke()),
            ("SelectionItemPattern", lambda: element.select()),
            ("TogglePattern", lambda: element.toggle()),
            ("ExpandCollapsePattern", lambda: element.expand()),
            ("LegacyIAccessible.DoDefaultAction",
             lambda: element.iface_legacy_iaccessible.DoDefaultAction()),
        ),
    )


def set_control_text(element, text: str, clear: bool = True) -> Dict[str, Any]:
    """Set an edit control's value via ValuePattern.

    ValuePattern replaces the whole value atomically, so it is both faster and
    far more reliable than synthesizing keystrokes - and it needs no focus.
    """
    def _value_pattern():
        element.set_edit_text(text) if clear else element.type_keys(text)

    # Otherwise a read-only box fails both patterns with a COMError and a
    # SendInput count, neither of which names the cause.
    try:
        from .uia import _pattern

        read_only = bool(_pattern(element, "Value").CurrentIsReadOnly)
    except Exception:
        read_only = False
    if read_only:
        raise ActionError("the control is read-only (ValuePattern.IsReadOnly)")

    return _try_patterns(
        element,
        (
            ("ValuePattern", _value_pattern),
            ("type_keys", lambda: element.type_keys(text, set_foreground=False)),
        ),
    )


def toggle_control(element) -> Dict[str, Any]:
    return _try_patterns(
        element,
        (
            ("TogglePattern", lambda: element.toggle()),
            ("InvokePattern", lambda: element.invoke()),
        ),
    )


def select_control(element) -> Dict[str, Any]:
    return _try_patterns(
        element,
        (
            ("SelectionItemPattern", lambda: element.select()),
            ("InvokePattern", lambda: element.invoke()),
        ),
    )


def expand_control(element, collapse: bool = False) -> Dict[str, Any]:
    return _try_patterns(
        element,
        (
            ("ExpandCollapsePattern",
             lambda: element.collapse() if collapse else element.expand()),
        ),
    )


SCROLL_UNAVAILABLE = (
    "Nothing in reach of this control exposes a ScrollPattern, and wheel input "
    "is not an option: SendInput needs the input queue, which a background "
    "desktop does not own, so it delivers zero events. Either scroll the inner "
    "ScrollViewer (get_controls with actionable_only=false, include_unnamed=true "
    "to see it), or bring the target into view with select_control - selecting "
    "an item scrolls it into view through UIA, which is how a virtualized list "
    "should be reached anyway."
)


# pywinauto's `scroll(count=N)` issues N separate COM Scroll calls with
# `Timings.scroll_step_wait` (0.1s) between them, against a UI thread that may be
# busy laying out what it just scrolled. `amount=40` is therefore not "scroll
# further", it is forty blocking round trips - which is how one call hung long
# enough for the client to abandon it and take the session with it. Ten lines is
# past any reasonable nudge; a real jump belongs in `to_percent`, which is one
# COM call whatever the distance.
MAX_SCROLL_LINES = 10
_SCROLL_STEP_WAIT = 0.02


def scroll_control(
    element,
    direction: str = "down",
    amount: int = 3,
    to_percent: Optional[float] = None,
) -> Dict[str, Any]:
    """Scroll via ScrollPattern, resolving to the nearest element that can scroll.

    There is deliberately no wheel fallback. `_wheel_at_element` goes through
    SendInput, which this module documents as inert off the input desktop, so
    offering it only turned "this control does not scroll" into a two-clause
    error ending in `GetLastError=5` - which reads like a permissions problem
    and is not one.

    The response carries where the scroller now sits (`vertical_percent`,
    `at_top`, `at_bottom`), because the alternative way to find that out is a
    screenshot, and "did I go far enough" is the question every scroll raises.
    """
    vertical = direction in ("down", "up")

    from .uia import NO_SCROLL, find_scrollable, scroll_interface, scroll_offsets

    target, where = find_scrollable(element, vertical=vertical)
    if target is None:
        raise ActionError(SCROLL_UNAVAILABLE)

    notes = []
    if to_percent is not None:
        percent = max(0.0, min(100.0, float(to_percent)))
        if percent != to_percent:
            notes.append(f"to_percent clamped to {percent}")

        def _do_scroll():
            iface = scroll_interface(target)
            if iface is None:
                raise ActionError("no ScrollPattern on the resolved scroller")
            if vertical:
                iface.SetScrollPercent(NO_SCROLL, percent)
            else:
                iface.SetScrollPercent(percent, NO_SCROLL)

        method_name = "ScrollPattern.SetScrollPercent"
    else:
        lines = max(1, min(MAX_SCROLL_LINES, int(amount)))
        if lines != amount:
            notes.append(
                f"amount clamped from {amount} to {lines} lines - use "
                "to_percent for a long jump, it is one call rather than one per line"
            )
        step = lines * (1 if direction in ("down", "right") else -1)

        def _do_scroll():
            if vertical:
                target.scroll(
                    "down" if step > 0 else "up", "line", abs(step),
                    retry_interval=_SCROLL_STEP_WAIT,
                )
            else:
                target.scroll(
                    "right" if step > 0 else "left", "line", abs(step),
                    retry_interval=_SCROLL_STEP_WAIT,
                )

        method_name = "ScrollPattern"

    iface = scroll_interface(target)
    before = scroll_offsets(iface) if iface is not None else {}
    outcome = _try_patterns(target, ((method_name, _do_scroll),))
    # Say which element actually moved: "it scrolled" is a different fact from
    # "the control you named scrolled".
    outcome["scrolled"] = where
    if where != "self":
        outcome["scrolled_element"] = {
            "name": _safe_text(target),
            "control_type": getattr(getattr(target, "element_info", None), "control_type", None),
            "class_name": getattr(getattr(target, "element_info", None), "class_name", None),
        }
    if iface is not None:
        want = percent if to_percent is not None else None
        outcome.update(_settled_offsets(iface, before, want, vertical))
    if notes:
        outcome["notes"] = notes
    return outcome


# How long a scroll's read-back may wait for the scroller to settle.
_SCROLL_SETTLE = 0.5


def _settled_offsets(iface, before: Dict[str, Any], want: Optional[float],
                     vertical: bool) -> Dict[str, Any]:
    """The scroller's offsets once the scroll has landed.

    Read straight after the pattern call they were often the position *before*
    it: `to_percent=30` answered 0 and `to_percent=0` answered 100, while the
    next listing showed the scroll had happened. The app applies it a moment
    later, most visibly right after a navigation or a layout change. So this
    waits until the requested percent is reached, or the offsets have moved
    and then held still for one read - and gives up after `_SCROLL_SETTLE`,
    which is what an already-at-the-end scroll costs.
    """
    from .uia import scroll_offsets

    key = "vertical_percent" if vertical else "horizontal_percent"
    deadline = time.time() + _SCROLL_SETTLE
    previous = None
    while True:
        current = scroll_offsets(iface)
        if want is not None and key in current and abs(current[key] - want) <= 0.5:
            return current
        if current != before and current == previous:
            return current
        if time.time() >= deadline:
            return current
        previous = current
        time.sleep(0.05)


def _rect_dict(element) -> Optional[Dict[str, int]]:
    try:
        r = element.rectangle()
        return {
            "left": r.left, "top": r.top, "right": r.right, "bottom": r.bottom,
            "width": r.width(), "height": r.height(),
        }
    except Exception:
        return None


def _contains(outer: Dict[str, int], inner: Dict[str, int]) -> bool:
    return (
        inner["left"] >= outer["left"] and inner["top"] >= outer["top"]
        and inner["right"] <= outer["right"] and inner["bottom"] <= outer["bottom"]
    )


def scroll_to_control(element, margin: int = 24, allow_select: bool = True) -> Dict[str, Any]:
    """Bring one control into view, whatever kind of control it is.

    Three routes, in order:

      1. `ScrollItemPattern.ScrollIntoView` - the pattern that exists for exactly
         this and is what a ScrollViewer's children expose.
      2. `SelectionItemPattern.select` - selecting scrolls an item into view, and
         is the only route that works on a virtualized list where the target is
         not realized yet. It was also the *only* route isodesk offered, which is
         why this function exists: an `Expander` or a `Group` is not selectable,
         so the previous advice ("use select_control") was false for them and the
         fallback was guessing a scroll amount and screenshotting to see where it
         landed.
      3. Arithmetic - work out where the element sits inside the scroller's
         content from the current percent and view size, and `SetScrollPercent`
         there. Approximate by construction, so it is last.

    Reports the rect before and after and whether the element is now inside the
    scroller's viewport, so the caller never has to take a picture to find out.

    `allow_select=False` drops route 2. Route 2 is the only one that changes
    application state - selecting a NavigationViewItem or a ListViewItem also
    *activates* it - which is acceptable when the caller asked to scroll and is
    not acceptable when the scroll is a side effect of taking a screenshot. The
    capture path passes False; a caller who asked for `scroll_to_control`
    directly gets all three routes.
    """
    from .uia import find_scrollable, scroll_interface, scroll_item_interface

    before = _rect_dict(element)
    scroller, where = find_scrollable(element, vertical=True)
    viewport = _rect_dict(scroller) if scroller is not None else None

    attempts = []

    def _record(name: str) -> Dict[str, Any]:
        after = _rect_dict(element)
        result: Dict[str, Any] = {
            "method": name,
            "attempts": attempts,
            "rect_before": before,
            "rect_after": after,
            "moved": bool(before and after and after != before),
        }
        if scroller is not None:
            result["scroller"] = where
            fresh_viewport = _rect_dict(scroller)
            if fresh_viewport and after:
                result["in_view"] = _contains(fresh_viewport, after)
            iface = scroll_interface(scroller)
            if iface is not None:
                from .uia import scroll_offsets

                result.update(scroll_offsets(iface))
        return result

    item_iface = scroll_item_interface(element)
    if item_iface is not None:
        try:
            item_iface.ScrollIntoView()
            time.sleep(0.15)
            return _record("ScrollItemPattern.ScrollIntoView")
        except Exception as exc:
            attempts.append(f"ScrollItemPattern: {type(exc).__name__}: {exc}")

    if allow_select:
        try:
            element.select()
            time.sleep(0.15)
            return _record("SelectionItemPattern")
        except Exception as exc:
            attempts.append(f"SelectionItemPattern: {type(exc).__name__}: {exc}")
    else:
        attempts.append("SelectionItemPattern: skipped (it would also activate)")

    if scroller is None or viewport is None or before is None:
        raise ActionError(
            "cannot scroll this control into view: it exposes no ScrollItemPattern, "
            "is not selectable, and nothing in reach exposes a ScrollPattern -> "
            + " | ".join(attempts)
        )

    iface = scroll_interface(scroller)
    percent = _percent_for(iface, viewport, before, margin)
    if percent is None:
        raise ActionError(
            "cannot compute a scroll position for this control (the scroller "
            "reports no usable view size) -> " + " | ".join(attempts)
        )
    from .uia import NO_SCROLL

    iface.SetScrollPercent(NO_SCROLL, percent)
    time.sleep(0.15)
    return _record("ScrollPattern.SetScrollPercent")


def _percent_for(iface, viewport: Dict[str, int], rect: Dict[str, int], margin: int):
    """Where to scroll so `rect` lands just inside `viewport`.

    UIA gives a percent and a view size, not pixel offsets, so the content height
    has to be reconstructed: the viewport shows `view_size` percent of it.
    """
    try:
        view_size = float(iface.CurrentVerticalViewSize)
        current = float(iface.CurrentVerticalScrollPercent)
    except Exception:
        return None
    if view_size <= 0 or view_size >= 100:
        return None

    viewport_height = viewport["height"]
    content_height = viewport_height / (view_size / 100.0)
    scrollable = content_height - viewport_height
    if scrollable <= 0:
        return None

    current_offset = max(0.0, current) / 100.0 * scrollable
    element_offset = (rect["top"] - viewport["top"]) + current_offset
    wanted = max(0.0, min(scrollable, element_offset - margin))
    return round(wanted / scrollable * 100.0, 2)


def _safe_text(element) -> str:
    try:
        return element.window_text()
    except Exception:
        return ""


def get_control_text(element) -> str:
    # A control with a ValuePattern answers with its value, even an empty one.
    # Falling through to the name on "" reported Monaco's empty editor as
    # holding the text "Editor content".
    try:
        value = element.get_value()
        if value is not None:
            return value
    except Exception:
        pass
    for getter in (
        lambda: element.window_text(),
        lambda: element.legacy_properties().get("Value"),
    ):
        try:
            value = getter()
            if value:
                return value
        except Exception:
            continue
    return ""


# ---------------------------------------------------------------------------
# coordinate addressing without the input desktop
# ---------------------------------------------------------------------------


def element_from_point(x: int, y: int):
    """Resolve the UIA element under a screen point on this desktop.

    This is how isodesk offers coordinate-addressed actions at all. SendInput
    and SetCursorPos only affect the *input* desktop, so a background desktop
    cannot receive synthetic hardware input - but UIA hit-testing works fine,
    and the element it returns can be invoked through a pattern.
    """
    from pywinauto.uia_defines import IUIA
    from pywinauto.uia_element_info import UIAElementInfo
    from pywinauto.controls.uiawrapper import UIAWrapper

    point = wintypes.POINT(int(x), int(y))
    try:
        raw = IUIA().iuia.ElementFromPoint(point)
    except Exception as exc:
        raise ActionError(f"ElementFromPoint({x},{y}) failed: {exc}") from exc
    if raw is None:
        raise ActionError(f"no UIA element at ({x},{y})")
    return UIAWrapper(UIAElementInfo(raw))


GA_ROOT = 2
WM_NCHITTEST = 0x0084
SMTO_ABORTIFHUNG = 0x0002

# WM_NCHITTEST codes worth refusing. HTCAPTION (the title bar itself) is not
# here: clicking it focuses or starts a drag and harms nothing, and refusing it
# would make the guard broader than the danger.
_FRAME_HITS = {
    3: "the system menu",
    8: "the Minimize button",
    9: "the Maximize button",
    20: "the Close button",
    21: "the Help button",
}


def frame_hit(x: int, y: int) -> Optional[str]:
    """Which part of a window's frame is at this point, if any.

    Asked of the window rather than of UIA, because UIA cannot answer it on
    WinUI 3: hit-testing anywhere in the caption strip returns a single
    `InputNonClientPointerSource` Pane spanning the whole strip, not the
    Minimize, Maximize or Close button that is visually there. A name-based
    guard - which is what `click_control` uses, and what `uia.is_caption_button`
    implements - therefore cannot fire on the coordinate path at all, however
    faithfully it is copied across.

    Returns None for a client-area point, an unknown code, or a window that does
    not answer in time, so a failure here never blocks an ordinary click.
    """
    point = wintypes.POINT(int(x), int(y))
    hwnd = user32.WindowFromPoint(point)
    if not hwnd:
        return None
    hwnd = user32.GetAncestor(hwnd, GA_ROOT) or hwnd

    result = ctypes.c_size_t(0)
    ok = user32.SendMessageTimeoutW(
        wintypes.HWND(hwnd), WM_NCHITTEST, 0, _lparam(x, y),
        SMTO_ABORTIFHUNG, 250, ctypes.byref(result),
    )
    if not ok:
        return None
    return _FRAME_HITS.get(int(result.value))


def click_at_uia(x: int, y: int) -> Dict[str, Any]:
    """Click a point by resolving the UIA element there and invoking it.

    Two things the caller cannot see from the outside, both now reported rather
    than left to be inferred:

      * **Retargeting.** `ElementFromPoint` hands back whatever UIA hit-testing
        decides is there, which is often an ancestor or a sibling of the thing
        visually under the cursor - and `invoke_control` then invokes *that*,
        not the point. Dismissing a flyout by clicking an inert label came back
        as `resolved: Close/Button`, which is a completely different action from
        the one that was asked for. When the resolved element's rect does not
        contain the point, `retargeted` says so. Silent retargeting is the
        dangerous shape; a named one is just information.
      * **Caption buttons.** The frame's own Minimize/Maximize/Close are
        hit-testable by coordinate like anything else, and invoking one hides or
        closes the window - after which every later capture comes back blank for
        a reason that has nothing to do with the app. `click_control` has
        refused them since it existed (`uia.is_caption_button`); the coordinate
        path walked straight past the same guard, so it now consults it too.

    `is_caption_button` keys on the *absent* class_name rather than the name, so
    a ComboBox light-dismiss Button named "Close" is not caught and dismissing a
    popup by coordinate still works.
    """
    from .uia import describe, is_caption_button

    hit = frame_hit(x, y)
    if hit:
        raise Refused(
            f"({x},{y}) is on {hit} of the window frame; clicking it would hide "
            "or close the window, after which every capture comes back blank for "
            "a reason that has nothing to do with the app. Use window_command if "
            "that is what you meant."
        )

    element = element_from_point(x, y)
    described = describe(element, "xy", include_state=False)

    if is_caption_button(described):
        raise Refused(
            f"({x},{y}) resolves to {described.get('name')!r}, one of the window "
            "frame's own caption buttons; invoking it would hide or close the "
            "window. Use window_command if that is what you meant, or click a "
            "point inside the app's content."
        )

    result: Dict[str, Any] = {
        "x": x,
        "y": y,
        "resolved": {
            "name": described.get("name"),
            "control_type": described.get("control_type"),
            "xaml_type": described.get("xaml_type"),
            "automation_id": described.get("automation_id"),
        },
    }
    rect = described.get("rect")
    if rect and not _rect_contains(rect, x, y):
        result["retargeted"] = {"requested": [x, y], "element_rect": rect}

    outcome = invoke_control(element)
    return {**outcome, **result}


def _rect_contains(rect: Dict[str, int], x: int, y: int) -> bool:
    return (
        rect["left"] <= x < rect["right"] and rect["top"] <= y < rect["bottom"]
    )


# ---------------------------------------------------------------------------
# PostMessage - works for classic Win32 controls; unreliable on WinUI 3
# ---------------------------------------------------------------------------

WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0201, 0x0202, 0x0203
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK = 0x0204, 0x0205, 0x0206
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
WM_MOUSEWHEEL = 0x020A
MK_LBUTTON = 0x0001


def _lparam(x: int, y: int) -> int:
    return (int(y) << 16) | (int(x) & 0xFFFF)


def _screen_to_client(hwnd: int, x: int, y: int) -> tuple:
    point = wintypes.POINT(int(x), int(y))
    user32.ScreenToClient(hwnd, ctypes.byref(point))
    return point.x, point.y


def postmessage_click(hwnd: int, x: int, y: int, button: str = "left",
                      double: bool = False) -> Dict[str, Any]:
    """Post a synthetic click to a window, in screen coordinates.

    `double` is honoured here and nowhere else: a UIA pattern invoke has no
    notion of a second click, so on the UIA path the flag has nothing to act on.
    It used to be accepted by `click_xy` and dropped on the floor by both paths.
    """
    cx, cy = _screen_to_client(hwnd, x, y)
    down, up, dbl = (
        (WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK) if button == "right"
        else (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK)
    )
    lp = _lparam(cx, cy)
    user32.PostMessageW(hwnd, down, MK_LBUTTON, lp)
    time.sleep(0.03)
    user32.PostMessageW(hwnd, up, 0, lp)
    if double:
        time.sleep(0.03)
        user32.PostMessageW(hwnd, dbl, MK_LBUTTON, lp)
        time.sleep(0.03)
        user32.PostMessageW(hwnd, up, 0, lp)
    return {
        "method": "PostMessage", "hwnd": hwnd, "client": [cx, cy],
        "button": button, "double": double,
    }


def postmessage_chars(hwnd: int, text: str) -> Dict[str, Any]:
    """Post WM_CHAR for each character to a window."""
    for char in text:
        user32.PostMessageW(hwnd, WM_CHAR, ord(char), 0)
        time.sleep(0.005)
    return {"method": "PostMessage/WM_CHAR", "hwnd": hwnd, "length": len(text)}


def postmessage_wheel(hwnd: int, x: int, y: int, clicks: int) -> Dict[str, Any]:
    wparam = (int(clicks) * 120) << 16
    user32.PostMessageW(hwnd, WM_MOUSEWHEEL, wparam, _lparam(int(x), int(y)))
    return {"method": "PostMessage/WM_MOUSEWHEEL", "hwnd": hwnd, "clicks": clicks}


# ---------------------------------------------------------------------------
# SendInput - retained for the record; does NOT work on a background desktop
# ---------------------------------------------------------------------------

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


def _send(*inputs: _INPUT) -> None:
    array = (_INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(_INPUT))
    if sent != len(inputs):
        raise ActionError(
            f"SendInput delivered {sent}/{len(inputs)} events "
            f"(GetLastError={ctypes.get_last_error()})"
        )


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> _INPUT:
    return _INPUT(
        type=INPUT_MOUSE,
        union=_INPUTUNION(mi=_MOUSEINPUT(dx, dy, data, flags, 0, None)),
    )


def click_at(x: int, y: int, button: str = "left", double: bool = False) -> Dict[str, Any]:
    """Click at absolute screen coordinates on the isolated desktop."""
    if not user32.SetCursorPos(int(x), int(y)):
        raise ActionError(f"SetCursorPos({x},{y}) failed")
    time.sleep(0.05)
    down, up = (
        (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP)
        if button == "right"
        else (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
    )
    _send(_mouse(down), _mouse(up))
    if double:
        time.sleep(0.05)
        _send(_mouse(down), _mouse(up))
    return {"method": "SendInput", "x": x, "y": y, "button": button, "double": double}


def drag_from_to(x1: int, y1: int, x2: int, y2: int, steps: int = 20) -> Dict[str, Any]:
    user32.SetCursorPos(int(x1), int(y1))
    time.sleep(0.05)
    _send(_mouse(MOUSEEVENTF_LEFTDOWN))
    for i in range(1, steps + 1):
        user32.SetCursorPos(
            int(x1 + (x2 - x1) * i / steps), int(y1 + (y2 - y1) * i / steps)
        )
        time.sleep(0.01)
    _send(_mouse(MOUSEEVENTF_LEFTUP))
    return {"method": "SendInput", "from": [x1, y1], "to": [x2, y2]}


def _wheel_at_element(element, clicks: int) -> None:
    """Inert on a background desktop; kept only so spikes can keep measuring it."""
    rect = element.rectangle()
    user32.SetCursorPos((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
    _send(_mouse(MOUSEEVENTF_WHEEL, data=clicks * 120))


def scroll_at(x: int, y: int, clicks: int) -> Dict[str, Any]:
    user32.SetCursorPos(int(x), int(y))
    _send(_mouse(MOUSEEVENTF_WHEEL, data=int(clicks) * 120))
    return {"method": "SendInput", "x": x, "y": y, "clicks": clicks}


def _raw_type_text(text: str, per_char_delay: float = 0.005) -> Dict[str, Any]:
    """SendInput-based typing. Does NOT work on a background desktop.

    Retained only so spikes/spike_input.py can keep measuring it. Never wire
    this into the MCP surface: it raises rather than typing, and there is no
    "whatever has focus" to type into anyway, because UIA's GetFocusedElement is
    not desktop-scoped (it reports the *user's* focused window).
    """
    for char in text:
        code = ord(char)
        down = _INPUT(
            type=INPUT_KEYBOARD,
            union=_INPUTUNION(ki=_KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, None)),
        )
        up = _INPUT(
            type=INPUT_KEYBOARD,
            union=_INPUTUNION(
                ki=_KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)
            ),
        )
        _send(down, up)
        if per_char_delay:
            time.sleep(per_char_delay)
    return {"method": "SendInput/unicode", "length": len(text)}


KEYBOARD_UNAVAILABLE = (
    "Synthetic keystrokes cannot be delivered to a background desktop. "
    "SendInput requires ownership of the input queue, which only the input "
    "desktop has, and PostMessage(WM_CHAR) is ignored by WinUI 3 - measured "
    "against every child HWND including DesktopChildSiteBridge and "
    "InputSiteWindowClass (spikes/spike_keyboard.py). "
    "Use set_text (UIA ValuePattern) to put text into an edit control, and "
    "click_control / select_control / expand_control to reach anything a "
    "shortcut would have triggered."
)


def send_keys(hwnd: int, keys: str) -> Dict[str, Any]:
    """Not supported on an isolated desktop; see KEYBOARD_UNAVAILABLE.

    pywinauto's `type_keys` goes through SendInput, which silently delivers zero
    events off the input desktop. Failing loudly here is better than an agent
    believing it pressed Ctrl+S.
    """
    raise ActionError(KEYBOARD_UNAVAILABLE)


def _raw_send_keys(hwnd: int, keys: str) -> Dict[str, Any]:
    """The SendInput path, kept only for the input-mechanism spike."""
    window = get_window(hwnd)
    window.type_keys(keys, set_foreground=False)
    return {"method": "type_keys", "keys": keys}
