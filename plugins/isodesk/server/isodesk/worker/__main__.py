"""Session worker: the process that lives on the isolated desktop.

Launched by the host with `STARTUPINFO.lpDesktop` pointing at the session's
desktop, so its COM apartment, UIA root, cursor and foreground window are all
natively scoped to that desktop. This is why isodesk uses a worker *process*
rather than `SetThreadDesktop` on a thread in the host: SetThreadDesktop fails
once a thread owns windows, hooks or an initialized COM apartment, and UIA is
COM.

Everything that touches the isolated desktop happens here. The host never calls
a UI API against the session directly.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

# Allow `python -m isodesk.worker` to work from a source checkout.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from isodesk import actions, capture, uia  # noqa: E402
from isodesk.ipc import PipeServer  # noqa: E402
from isodesk.win32 import desktop as dt  # noqa: E402
from isodesk.win32 import process as pr  # noqa: E402


class WorkerState:
    """Per-session state that must survive across RPC calls."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.hdesk = dt.current_thread_desktop()
        self.launched_pids: List[int] = []
        # Snapshot of the last control enumeration, so that `click_control("c7")`
        # refers to the same list the caller actually saw.
        self.controls: Dict[int, List[Dict[str, Any]]] = {}
        # Which listing that snapshot was, per window. Ids are positional, so
        # `c23` from a scoped 12-row listing and `c23` from a full 40-row one are
        # different controls with the same name. Handing back a token the caller
        # can quote turns that from a silent wrong action into a refusal.
        self.listings: Dict[int, str] = {}
        self.listing_seq = 0
        self.active_hwnd: Optional[int] = None

    def store_listing(self, hwnd: int, controls: List[Dict[str, Any]]) -> str:
        self.listing_seq += 1
        token = f"L{self.listing_seq}"
        self.controls[hwnd] = controls
        self.listings[hwnd] = token
        return token


def _resolve_hwnd(state: WorkerState, hwnd: Optional[int]) -> int:
    """Default to the session's single app window when the caller omits one."""
    if hwnd:
        return int(hwnd)
    if state.active_hwnd and dt.window_title(state.active_hwnd):
        return state.active_hwnd
    windows = dt.app_windows(state.hdesk)
    if not windows:
        raise RuntimeError(
            "no application window on this session's desktop - launch an app first"
        )
    state.active_hwnd = windows[0]["hwnd"]
    return state.active_hwnd


# ---------------------------------------------------------------------------
# RPC handlers
# ---------------------------------------------------------------------------


def h_ping(state: WorkerState, **_) -> Dict[str, Any]:
    return {
        "pid": os.getpid(),
        "session_id": state.session_id,
        "hdesk": state.hdesk,
        "windows": len(dt.app_windows(state.hdesk)),
    }


def h_launch(state: WorkerState, cmdline: str, cwd: Optional[str] = None,
             wait: float = 3.0, **_) -> Dict[str, Any]:
    """Launch an unpackaged executable onto this desktop."""
    before = {w["hwnd"] for w in dt.app_windows(state.hdesk)}
    pid = pr.launch_on_desktop(cmdline, dt.desktop_path(state.session_id), cwd=cwd)
    state.launched_pids.append(pid)
    new = _wait_for_new_window(state, before, wait)
    return {
        "pid": pid,
        "cmdline": cmdline,
        "new_windows": new,
        # Which binary is actually running - the launcher pid often is not it.
        "module": pr.module_info(new[0]["pid"] if new else pid),
    }


def h_activate(state: WorkerState, aumid: str, arguments: str = "",
               wait: float = 8.0, **_) -> Dict[str, Any]:
    """Activate a packaged (MSIX/Store) app onto this desktop.

    Works because this call originates from a process already on the isolated
    desktop; the activation infrastructure places the app on the caller's
    desktop.
    """
    before = {w["hwnd"] for w in dt.app_windows(state.hdesk)}
    pid = pr.activate_packaged_app(aumid, arguments)
    state.launched_pids.append(pid)
    new = _wait_for_new_window(state, before, wait)
    return {
        "pid": pid,
        "aumid": aumid,
        "new_windows": new,
        "module": pr.module_info(new[0]["pid"] if new else pid),
    }


def _wait_for_new_window(state: WorkerState, before: set, timeout: float) -> List[Dict[str, Any]]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = dt.app_windows(state.hdesk)
        new = [w for w in current if w["hwnd"] not in before]
        if new:
            state.active_hwnd = new[0]["hwnd"]
            return new
        time.sleep(0.3)
    return []


def h_list_windows(state: WorkerState, **_) -> Dict[str, Any]:
    windows = dt.app_windows(state.hdesk)
    for window in windows:
        scale = dt.dpi_scale_for_window(window["hwnd"])
        window["dpi_scale"] = scale
        window["rect_dips"] = dt.to_dips(window["rect"], scale)
    if windows and not state.active_hwnd:
        state.active_hwnd = windows[0]["hwnd"]
    return {"windows": windows, "active_hwnd": state.active_hwnd}


def h_screenshot(state: WorkerState, hwnd: Optional[int] = None,
                 annotated: bool = False, max_width: Optional[int] = None,
                 control_id: Optional[str] = None, crop: Optional[List[int]] = None,
                 padding: int = 8, units: str = "dips",
                 scroll_into_view: bool = True, listing: Optional[str] = None,
                 **_) -> Dict[str, Any]:
    target = _resolve_hwnd(state, hwnd)

    # Scrolling has to happen before the capture, not before the crop: it moves
    # the whole frame, so cropping a frame taken beforehand would cut the old
    # position out of a picture that no longer shows it there.
    scrolled = None
    if control_id and scroll_into_view:
        scrolled = _bring_into_view(state, target, control_id, listing)

    image, quality = capture.capture_window_checked(target)
    rect = dt.window_rect(target)
    scale = dt.dpi_scale_for_window(target)
    result: Dict[str, Any] = {
        "hwnd": target,
        "title": dt.window_title(target),
        "rect": rect,
        "rect_dips": dt.to_dips(rect, scale),
        "dpi_scale": scale,
        "quality": quality,
    }
    if scrolled:
        result["scrolled_into_view"] = scrolled

    # Most flyouts, menus and drop-downs open in a *separate* top-level window,
    # so PrintWindow on the parent cannot contain them: the frame comes back with
    # the drop-down absent and the ComboBox looking shut. Reporting the popups is
    # the difference between "the flyout never opened" - a wrong conclusion that
    # is easy to reach and expensive to unwind - and one more call against the
    # popup's own hwnd. They are not composited into the image: a popup may
    # extend well beyond the parent's bounds (a light-dismiss layer covers the
    # whole desktop), and a stitched frame would misrepresent what any single
    # window actually rendered.
    #
    # Not every popup takes that route. A WinUI 3 Flyout is sometimes hosted
    # inside the parent's own content, with no popup window at all - so an empty
    # list here does not mean nothing opened, and the discriminator is the
    # parent's UIA tree, which contains an in-frame flyout's items.
    popups = [
        {
            "hwnd": w["hwnd"],
            "rect": w["rect"],
            "rect_dips": dt.to_dips(w["rect"], dt.dpi_scale_for_window(w["hwnd"])),
        }
        for w in dt.app_windows(state.hdesk)
        if w.get("popup") and w["pid"] == dt.window_pid(target) and w["hwnd"] != target
    ]
    if popups:
        result["popups"] = popups

    if annotated:
        enumerated = uia.list_controls(target)
        controls = enumerated["controls"]
        result["listing"] = state.store_listing(target, controls)
        image = capture.annotate(image, controls, (rect["left"], rect["top"]))
        result["controls"] = controls
        result["truncated"] = enumerated["truncated"]
        result["total_matched"] = enumerated["total_matched"]

    # Cropping happens after annotation so the boxes still land where they should,
    # and before encoding, which is where the tokens actually are.
    region, requested = _crop_region(
        state, target, scale, control_id, crop, units, listing
    )
    if region:
        padding_px = round(padding * scale) if units == "dips" else padding
        image, crop_info = capture.crop_to(
            image, (rect["left"], rect["top"]), region, padding_px
        )
        result.update(crop_info)
        if requested:
            result["crop_requested"] = requested
        hint = _crop_hint(control_id, crop_info, scrolled, requested, scroll_into_view)
        if hint:
            result["crop_hint"] = hint

    # The downscale is the third frame the caller never sees: it happens after
    # the crop, so the crop's own width and height describe an image nobody
    # received. Report what was actually delivered.
    before_width = image.width
    image = capture.downscale(image, max_width)
    result["delivered"] = {
        "width": image.width,
        "height": image.height,
        "scale": round(image.width / before_width, 3) if before_width else 1.0,
    }

    result["image_base64"] = capture.to_png_base64(image, None)
    return result


def _bring_into_view(state: WorkerState, target: int, control_id: str,
                     listing: Optional[str]) -> Optional[Dict[str, Any]]:
    """Scroll a control into view before capturing it, and refresh its cached rect.

    UIA's BoundingRectangle is already *clipped* by any scroll viewport above the
    control, so a 44x44 colour swatch cut to four pixels honestly reports 44x4 -
    and cropping to that returns a sliver of nothing, with a message saying only
    that part of the control is outside the window. The tool already knew how to
    fix the problem it was handing back; now it does, before the capture rather
    than after.

    The activating route in `scroll_to_control` is disabled here.
    SelectionItemPattern scrolls by *selecting*, which on a NavigationViewItem or
    a ListViewItem also invokes it - acceptable when the caller asked to scroll,
    not acceptable as a side effect of taking a picture.
    """
    try:
        controls = _controls_for(state, target, listing)
        described = next((c for c in controls if c["id"] == control_id), None)
        if described is None:
            # _crop_region raises the useful "unknown control id" a moment later.
            return None
        element, _ = uia.resolve_control(target, control_id, controls)
        outcome = actions.scroll_to_control(element, allow_select=False)
    except Exception as exc:
        return {"moved": False, "error": f"{type(exc).__name__}: {exc}"}

    after = outcome.get("rect_after")
    if after:
        described["rect"] = after
        scale = dt.dpi_scale_for_window(target)
        if scale:
            described["rect_dips"] = dt.to_dips(after, scale)
    return {
        "moved": bool(outcome.get("moved")),
        "method": outcome.get("method"),
        "rect_before": outcome.get("rect_before"),
        "rect_after": after,
        "in_view": outcome.get("in_view"),
    }


def _crop_hint(control_id: Optional[str], crop_info: Dict[str, Any],
               scrolled: Optional[Dict[str, Any]], requested: Optional[Dict[str, Any]],
               scroll_into_view: bool) -> str:
    """Say what to do about a mostly-clamped crop, not only that it happened.

    A crop that came back as a few percent of what was asked for is the one case
    where the image is guaranteed useless, and it used to carry the same sentence
    as a crop that lost eight pixels of padding.
    """
    fraction = crop_info.get("clamped_fraction", 1.0)
    if not control_id or fraction >= 0.5:
        return ""

    size = ""
    if requested:
        size = (
            f", whose rect is {requested['width']}x{requested['height']} "
            f"{requested.get('units', 'px')}"
        )
    head = f"only {round(fraction * 100)}% of {control_id}{size} lies inside the window"

    if not scroll_into_view:
        return (
            head + " - a control clipped by a scroll viewport reports the clipped "
            "rect, so it crops to a sliver. Drop scroll_into_view=false, or call "
            "scroll_to_control first."
        )
    if scrolled and scrolled.get("error"):
        return (
            head + f" - scroll_into_view could not run ({scrolled['error']}). Call "
            "scroll_to_control and capture again."
        )
    return (
        head + " - it was scrolled into view before this capture and is still "
        "clipped, so it is cut by a viewport that cannot scroll further, or it is "
        "genuinely off-window."
    )


def _crop_region(state: WorkerState, target: int, scale: float,
                 control_id: Optional[str], crop: Optional[List[int]],
                 units: str, listing: Optional[str]):
    """The screen-pixel rect to crop to, and the request echoed in its own frame.

    Returns `(region_px, requested)`. `region_px` is screen-absolute physical
    pixels, which is what `capture.crop_to` offsets by the window origin.

    `requested` is the same rectangle in the units the caller supplied, and it
    exists because the response used to echo only the window-relative post-clamp
    pixel rect - a frame the caller never provided and cannot use, which read as
    a contradiction of the screen-absolute frame the input actually takes.
    Explicit coordinates are in DIPs by default, matching what every listing
    reports and what a XAML author reasons in; `units="px"` takes them raw.
    """
    if control_id:
        controls = _controls_for(state, target, listing)
        described = next((c for c in controls if c["id"] == control_id), None)
        if described is None:
            raise RuntimeError(
                f"unknown control id {control_id!r} for this window's listing"
            )
        # The cached rect is enough here: a stale one crops the wrong region,
        # which is visible in the image rather than silently wrong. When
        # scroll_into_view ran, _bring_into_view has already refreshed it.
        rect = described.get("rect")
        if not rect:
            raise RuntimeError(f"control {control_id!r} has no rect to crop to")
        echo = described.get("rect_dips") if units == "dips" else rect
        echo_units = "dips" if (units == "dips" and described.get("rect_dips")) else "px"
        requested = {
            "left": (echo or rect)["left"], "top": (echo or rect)["top"],
            "width": (echo or rect)["width"], "height": (echo or rect)["height"],
            "units": echo_units,
            "source": f"rect of {control_id}",
        }
        return rect, requested

    if not crop:
        return None, None
    if len(crop) != 4:
        raise RuntimeError("crop must be [x, y, width, height]")
    x, y, width, height = crop
    if units == "dips":
        x, y, width, height = (round(v * scale) for v in (x, y, width, height))
    elif units != "px":
        raise RuntimeError(f"units must be 'dips' or 'px', got {units!r}")
    region = {
        "left": x, "top": y, "right": x + width, "bottom": y + height,
        "width": width, "height": height,
    }
    requested = {
        "left": crop[0], "top": crop[1], "width": crop[2], "height": crop[3],
        "units": units, "source": "crop",
    }
    return region, requested


def h_get_controls(state: WorkerState, hwnd: Optional[int] = None,
                   actionable_only: bool = True, include_unnamed: bool = False,
                   include_state: bool = True, scope: Optional[str] = None,
                   xaml_type: Optional[str] = None, control_type: Optional[str] = None,
                   name_contains: Optional[str] = None, **_) -> Dict[str, Any]:
    target = _resolve_hwnd(state, hwnd)
    root = None
    if scope:
        root = uia.resolve_scope(target, scope, state.controls.get(target) or [])
    listing = uia.list_controls(
        target,
        actionable_only=actionable_only,
        include_unnamed=include_unnamed,
        include_state=include_state,
        root=root,
        xaml_type=xaml_type,
        control_type=control_type,
        name_contains=name_contains,
    )
    token = state.store_listing(target, listing["controls"])
    payload = {"hwnd": target, "listing": token, **listing}
    if scope:
        payload["scope"] = scope
    return payload


def h_refresh_controls(state: WorkerState, ids: List[str], hwnd: Optional[int] = None,
                       listing: Optional[str] = None, include_state: bool = True,
                       **_) -> Dict[str, Any]:
    target = _resolve_hwnd(state, hwnd)
    controls = _controls_for(state, target, listing)
    payload = uia.refresh_controls(target, ids, controls, include_state=include_state)
    payload["listing"] = state.listings.get(target)
    return payload


def h_measure(state: WorkerState, control_ids: List[str], hwnd: Optional[int] = None,
              units: str = "dips", listing: Optional[str] = None, **_) -> Dict[str, Any]:
    target = _resolve_hwnd(state, hwnd)
    controls = _controls_for(state, target, listing)
    return uia.measure(target, control_ids, controls, units=units)


def h_audit_accessibility(state: WorkerState, hwnd: Optional[int] = None, **_) -> Dict[str, Any]:
    return uia.audit_accessibility(_resolve_hwnd(state, hwnd))


def h_get_control_tree(state: WorkerState, hwnd: Optional[int] = None,
                       max_depth: int = 6, **_) -> Dict[str, Any]:
    target = _resolve_hwnd(state, hwnd)
    return {"hwnd": target, "tree": uia.control_tree(target, max_depth=max_depth)}


def _controls_for(state: WorkerState, target: int, listing: Optional[str] = None
                  ) -> List[Dict[str, Any]]:
    """The snapshot an id addresses, refusing an id from a superseded listing.

    Positional ids are only meaningful against the listing that produced them,
    and scoping and filtering make that sharper: `c3` of a listing filtered to
    Expanders is a different control from `c3` of the full page. When the caller
    quotes the token it was given, a mismatch is caught here instead of resolving
    against whatever now holds that position.
    """
    controls = state.controls.get(target)
    if not controls:
        controls = uia.list_controls(target)["controls"]
        state.store_listing(target, controls)
    current = state.listings.get(target)
    if listing and current and listing != current:
        raise RuntimeError(
            f"that id came from listing {listing}, but the current listing for "
            f"this window is {current}. Re-read it (get_controls), or use "
            "refresh_controls to check specific ids."
        )
    return controls


def _element_for(state: WorkerState, hwnd: Optional[int], control_id: str,
                 listing: Optional[str] = None):
    target = _resolve_hwnd(state, hwnd)
    controls = _controls_for(state, target, listing)
    element, described = uia.resolve_control(target, control_id, controls)
    return target, element, described


def h_click_control(state: WorkerState, control_id: str, hwnd: Optional[int] = None,
                    listing: Optional[str] = None, **_) -> Dict[str, Any]:
    target, element, described = _element_for(state, hwnd, control_id, listing)
    if described.get("caption_button"):
        raise RuntimeError(
            f"{described['name']!r} is a window caption button; invoking it would "
            "hide or close the window. Use window_command instead if intended."
        )
    outcome = actions.invoke_control(element)
    return {"hwnd": target, "control": described, **outcome}


def h_set_text(state: WorkerState, control_id: str, text: str,
               hwnd: Optional[int] = None, clear: bool = True,
               listing: Optional[str] = None, **_) -> Dict[str, Any]:
    target, element, described = _element_for(state, hwnd, control_id, listing)
    outcome = actions.set_control_text(element, text, clear=clear)
    return {"hwnd": target, "control": described, **outcome}


def h_toggle_control(state: WorkerState, control_id: str, hwnd: Optional[int] = None,
                     listing: Optional[str] = None, **_) -> Dict[str, Any]:
    target, element, described = _element_for(state, hwnd, control_id, listing)
    return {"hwnd": target, "control": described, **actions.toggle_control(element)}


def h_select_control(state: WorkerState, control_id: str, hwnd: Optional[int] = None,
                     listing: Optional[str] = None, **_) -> Dict[str, Any]:
    target, element, described = _element_for(state, hwnd, control_id, listing)
    return {"hwnd": target, "control": described, **actions.select_control(element)}


def h_expand_control(state: WorkerState, control_id: str, collapse: bool = False,
                     hwnd: Optional[int] = None, listing: Optional[str] = None,
                     **_) -> Dict[str, Any]:
    target, element, described = _element_for(state, hwnd, control_id, listing)
    return {"hwnd": target, "control": described,
            **actions.expand_control(element, collapse=collapse)}


def h_get_text(state: WorkerState, control_id: str, hwnd: Optional[int] = None,
               listing: Optional[str] = None, **_) -> Dict[str, Any]:
    target, element, described = _element_for(state, hwnd, control_id, listing)
    return {"hwnd": target, "control": described, "text": actions.get_control_text(element)}


def h_scroll_control(state: WorkerState, control_id: str, direction: str = "down",
                     amount: int = 3, hwnd: Optional[int] = None,
                     to_percent: Optional[float] = None, listing: Optional[str] = None,
                     **_) -> Dict[str, Any]:
    target, element, described = _element_for(state, hwnd, control_id, listing)
    return {"hwnd": target, "control": described,
            **actions.scroll_control(element, direction=direction, amount=amount,
                                     to_percent=to_percent)}


def h_scroll_to_control(state: WorkerState, control_id: str, hwnd: Optional[int] = None,
                        listing: Optional[str] = None, **_) -> Dict[str, Any]:
    target, element, described = _element_for(state, hwnd, control_id, listing)
    return {"hwnd": target, "control": described,
            **actions.scroll_to_control(element)}


CLICK_METHODS = ("auto", "uia", "postmessage")


def h_click_xy(state: WorkerState, x: int, y: int, button: str = "left",
               double: bool = False, method: str = "auto", **_) -> Dict[str, Any]:
    """Coordinate click: UIA hit-test and pattern invoke, PostMessage as fallback.

    SendInput cannot be used: a background desktop does not own the input queue,
    so synthetic hardware input is discarded (measured in spikes/spike_input.py).

    `method` exists because the two rungs of the ladder are not equivalent.
    WinUI 3 routes pointer input through its input-site bridge and routinely
    ignores a posted WM_LBUTTONDOWN while PostMessage still returns success - so
    "auto" can report a click that never happened. `method="uia"` refuses rather
    than falling back; `method="postmessage"` skips straight to the fallback for
    a classic Win32 control that has no usable pattern.

    A refusal (`actions.Refused` - today, the caption-button guard) is never
    swallowed by the fallback: posting the click that UIA just declined to make
    would land on the Close button the guard exists to protect.
    """
    if method not in CLICK_METHODS:
        raise RuntimeError(
            f"method must be one of {CLICK_METHODS}, got {method!r}"
        )

    def _posted() -> Dict[str, Any]:
        hwnd = _resolve_hwnd(state, None)
        return actions.postmessage_click(hwnd, x, y, button=button, double=double)

    if method == "postmessage":
        return {**_posted(), "method_requested": method}

    try:
        return {**actions.click_at_uia(x, y), "method_requested": method}
    except actions.Refused:
        raise
    except Exception as uia_error:
        if method == "uia":
            raise
        result = _posted()
        result["uia_fallback_reason"] = str(uia_error)
        result["method_requested"] = method
        return result


# -- raw mechanism handlers, used by spikes/spike_input.py to measure what works


def h_raw_sendinput_click(state: WorkerState, x: int, y: int, **_) -> Dict[str, Any]:
    return actions.click_at(x, y)


def h_postmessage_click(state: WorkerState, hwnd: int, x: int, y: int, **_) -> Dict[str, Any]:
    rect = dt.window_rect(hwnd)
    return actions.postmessage_click(hwnd, rect["left"] + x, rect["top"] + y)


def h_postmessage_chars(state: WorkerState, hwnd: int, text: str, **_) -> Dict[str, Any]:
    return actions.postmessage_chars(hwnd, text)


def h_child_windows(state: WorkerState, hwnd: Optional[int] = None, **_) -> Dict[str, Any]:
    """Enumerate child HWNDs - WinUI 3 hosts its content in a child site bridge."""
    import win32gui

    target = _resolve_hwnd(state, hwnd)
    children = []

    def _cb(child, _lparam):
        children.append({
            "hwnd": child,
            "class": dt.window_class(child),
            "title": dt.window_title(child),
            "rect": dt.window_rect(child),
        })
        return True

    win32gui.EnumChildWindows(target, _cb, None)
    return {"hwnd": target, "children": children}


def h_focused_element(state: WorkerState, **_) -> Dict[str, Any]:
    """Report what UIA considers focused on this desktop."""
    from pywinauto.uia_defines import IUIA

    element = IUIA().iuia.GetFocusedElement()
    return {
        "name": element.CurrentName,
        "control_type": element.CurrentControlType,
        "native_hwnd": element.CurrentNativeWindowHandle,
        "automation_id": element.CurrentAutomationId,
    }


def h_element_at(state: WorkerState, x: int, y: int, **_) -> Dict[str, Any]:
    """Describe what UIA hit-testing returns at a point, without invoking it.

    The read half of click_xy. It exists because "click_xy landed somewhere
    else" and "click_xy refused" are both statements about this one call, and
    there was no way to look at it without also performing the click.
    """
    element = actions.element_from_point(x, y)
    described = uia.describe(element, "xy", include_state=False)
    described["caption_button"] = uia.is_caption_button(described)
    return {
        "point": [x, y],
        "resolved": described,
        # What the window itself says is there, which on WinUI 3 is the only one
        # of the two that can name a caption button.
        "frame_hit": actions.frame_hit(x, y),
    }


def h_send_keys(state: WorkerState, keys: str, hwnd: Optional[int] = None, **_) -> Dict[str, Any]:
    return actions.send_keys(_resolve_hwnd(state, hwnd), keys)


def h_raw_send_keys(state: WorkerState, keys: str, hwnd: Optional[int] = None, **_) -> Dict[str, Any]:
    return actions._raw_send_keys(_resolve_hwnd(state, hwnd), keys)


def h_window_command(state: WorkerState, command: str, hwnd: Optional[int] = None,
                     x: int = 0, y: int = 0, width: int = 0, height: int = 0,
                     units: str = "dips", **_) -> Dict[str, Any]:
    target = _resolve_hwnd(state, hwnd)
    scale = dt.dpi_scale_for_window(target)

    if command == "restore":
        dt.restore_window(target)
    elif command == "move":
        # Default to DIPs: every adaptive threshold an author writes in XAML is
        # in effective pixels, so "resize to 640 wide to trip the narrow state"
        # means 640 DIPs, not 640 physical pixels.
        if units == "dips":
            x, y = round(x * scale), round(y * scale)
            width, height = round(width * scale), round(height * scale)
        elif units != "px":
            raise RuntimeError(f"units must be 'dips' or 'px', got {units!r}")
        dt.move_window(target, x, y, width, height)
    else:
        raise RuntimeError(f"unknown window command {command!r}")

    rect = dt.window_rect(target)
    return {
        "hwnd": target,
        "command": command,
        "units": units,
        "dpi_scale": scale,
        "rect": rect,
        "rect_dips": dt.to_dips(rect, scale),
    }


def h_run(state: WorkerState, cmdline: str, timeout: float = 60.0,
          cwd: Optional[str] = None, **_) -> Dict[str, Any]:
    """Run a console command on this desktop and capture its output."""
    import subprocess

    completed = subprocess.run(
        cmdline, shell=True, capture_output=True, text=True, timeout=timeout, cwd=cwd
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout[-20000:],
        "stderr": completed.stderr[-20000:],
    }


def h_shutdown(state: WorkerState, **_) -> Dict[str, Any]:
    return {"stopping": True}


HANDLERS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "ping": h_ping,
    "launch": h_launch,
    "activate": h_activate,
    "run": h_run,
    "list_windows": h_list_windows,
    "screenshot": h_screenshot,
    "get_controls": h_get_controls,
    "get_control_tree": h_get_control_tree,
    "audit_accessibility": h_audit_accessibility,
    "get_text": h_get_text,
    "click_control": h_click_control,
    "set_text": h_set_text,
    "toggle_control": h_toggle_control,
    "select_control": h_select_control,
    "expand_control": h_expand_control,
    "scroll_control": h_scroll_control,
    "scroll_to_control": h_scroll_to_control,
    "refresh_controls": h_refresh_controls,
    "measure": h_measure,
    "click_xy": h_click_xy,
    "send_keys": h_send_keys,
    # Underscore-prefixed handlers exist so spikes/ can keep measuring the
    # mechanisms that do not work; they are not part of the MCP surface.
    "_raw_sendinput_click": h_raw_sendinput_click,
    "_raw_send_keys": h_raw_send_keys,
    "_postmessage_click": h_postmessage_click,
    "_postmessage_chars": h_postmessage_chars,
    "_child_windows": h_child_windows,
    "_focused_element": h_focused_element,
    "_element_at": h_element_at,
    "window_command": h_window_command,
    "shutdown": h_shutdown,
}


def _reap_launched(state: WorkerState) -> List[int]:
    """Kill the apps this worker launched, on the way out.

    The worker exits when its pipe dies, which is exactly the case a clean
    `session_destroy` never reaches: the host was killed, so nothing on the host
    side will ever sweep this desktop. The app left behind is invisible and holds
    its own binaries open, which turns the next build into a lock that retries
    silently for minutes.

    Safety rule, the same one `kill_desktop_processes` enforces: a pid that also
    owns a window on the *Default* desktop is a host process - a single-instance
    app such as Windows 11 Notepad hands off to a copy already running there -
    and is never touched. If that list cannot be read, nothing is killed. Leaking
    a process is recoverable; killing the user's editor is not.
    """
    if not state.launched_pids:
        return []

    hdesk = None
    try:
        hdesk = dt.open_desktop("Default")
        host_pids = {
            w["pid"] for w in dt.enum_desktop_windows(hdesk, visible_only=False)
        }
    except Exception:
        return []
    finally:
        if hdesk:
            try:
                dt.close_desktop(hdesk)
            except Exception:
                pass

    killed = []
    for pid in set(state.launched_pids):
        if pid in host_pids or pid == os.getpid():
            continue
        if pr.kill_process_tree(pid):
            killed.append(pid)
    return killed


def serve(session_id: str, log_path: Optional[str] = None) -> int:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware

    def log(message: str) -> None:
        if not log_path:
            return
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%H:%M:%S')}] {message}\n")
        except Exception:
            pass

    state = WorkerState(session_id)
    log(f"worker starting pid={os.getpid()} hdesk={state.hdesk}")

    server = PipeServer(session_id).create()
    log(f"pipe created: {server.name}")
    server.accept()
    log("host connected")

    try:
        while True:
            try:
                request = server.recv()
            except Exception as exc:
                log(f"receive failed, exiting: {exc}")
                break

            method = request.get("method", "")
            params = request.get("params") or {}
            handler = HANDLERS.get(method)
            if handler is None:
                server.send({"id": request.get("id"), "ok": False,
                             "error": f"unknown method {method!r}"})
                continue

            try:
                result = handler(state, **params)
                server.send({"id": request.get("id"), "ok": True, "result": result})
            except Exception as exc:
                log(f"{method} raised: {traceback.format_exc()}")
                server.send({
                    "id": request.get("id"), "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue

            if method == "shutdown":
                log("shutdown requested")
                break
    finally:
        server.close()
        killed = _reap_launched(state)
        log(f"worker exiting; reaped {killed}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="isodesk.worker")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--log")
    args = parser.parse_args()
    return serve(args.session_id, args.log)


if __name__ == "__main__":
    sys.exit(main())
