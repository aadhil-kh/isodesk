"""UIA control discovery on the isolated desktop.

Runs only inside the worker process, which is already attached to the isolated
desktop, so pywinauto's `Desktop(backend="uia")` root resolves to that desktop's
windows and nothing else. Phase 0 measured 224 descendants on the WinUI 3 Gallery
window this way, with names and AutomationIds populated.

Control filtering keeps the control types an agent can actually act on and drops
pure layout containers, which otherwise dominate a XAML tree.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Control types worth showing an agent. A WinUI 3 tree is mostly Panes, Groups
# and Custom elements used for layout; surfacing those buries the real targets.
ACTIONABLE_TYPES = {
    "Button", "CheckBox", "ComboBox", "Edit", "Hyperlink", "ListItem",
    "MenuItem", "RadioButton", "Slider", "Spinner", "TabItem", "Text",
    "TreeItem", "ToolBar", "Document", "SplitButton", "Custom",
}

# Never offer these as click targets: invoking a caption button silently hides
# or closes the window and every later capture comes back blank.
CAPTION_CONTROLS = {"Minimize", "Maximize", "Restore", "Close"}

# Popup surfaces (flyout, menu, combo drop-down, tooltip) are usually hosted in
# their own top-level window rather than inside the parent, so they are reached
# by hwnd. A XAML Flyout hosted in-frame has no such window and is instead simply
# present in the parent's own control tree.
POPUP_WINDOW_CLASSES = {"Microsoft.UI.Content.PopupWindowSiteBridge"}

# XAML types arrive as class_name, sometimes bare ("ToggleSwitch") and sometimes
# fully qualified ("Microsoft.UI.Xaml.Controls.SelectorBarItem"). Longest prefix
# first so the Primitives namespace is not left behind by the shorter match.
_XAML_PREFIXES = (
    "Microsoft.UI.Xaml.Controls.Primitives.",
    "Microsoft.UI.Xaml.Controls.",
    "CommunityToolkit.WinUI.Controls.",
    "CommunityToolkit.WinUI.UI.Controls.",
    "Windows.UI.Xaml.Controls.Primitives.",
    "Windows.UI.Xaml.Controls.",
)

# UIA ExpandCollapseState
_EXPAND_STATES = {0: "collapsed", 1: "expanded", 2: "partially_expanded", 3: "leaf"}
# UIA ToggleState
_TOGGLE_STATES = {0: "off", 1: "on", 2: "indeterminate"}


class UiaError(RuntimeError):
    pass


def xaml_type_of(class_name: Optional[str]) -> Optional[str]:
    """Normalize `class_name` to the XAML type an author would recognize.

    UIA's `control_type` is lossy on WinUI 3 - a ToggleSwitch and an Expander are
    both reported as `Button`, a SelectorBarItem and a NavigationViewItem both as
    `ListItem` - so it cannot express "every toggle switch on this page".
    `class_name` carries the real type; this just strips the namespace.
    """
    if not class_name:
        return None
    for prefix in _XAML_PREFIXES:
        if class_name.startswith(prefix):
            return class_name[len(prefix):]
    return class_name


# Which controls are worth asking for which pattern. Gating matters more than it
# looks: an unsupported probe raises fast, but a *supported* one is a real COM
# property read, so the cost is concentrated in the controls that answer. Probing
# all four on every survivor measured 2.42x the cost of a plain enumeration on
# the Gallery; gating brings it back under the "must not double" bar.
#
# Both dimensions are checked because UIA's control_type is lossy on WinUI 3 -
# ToggleSwitch and Expander both report as Button - so xaml_type is what
# distinguishes them.
_TOGGLE_TYPES = {"CheckBox", "RadioButton"}
_TOGGLE_XAML = {
    "ToggleSwitch", "ToggleButton", "CheckBox", "AppBarToggleButton",
    "ToggleMenuFlyoutItem", "RadioMenuFlyoutItem",
}
_SELECT_TYPES = {"ListItem", "TabItem", "RadioButton", "TreeItem"}
_SELECT_XAML = {
    "NavigationViewItem", "SelectorBarItem", "ListViewItem", "GridViewItem",
    "TabViewItem", "PivotItem", "ComboBoxItem", "RadioButton", "ListBoxItem",
}
_EXPAND_TYPES = {"ComboBox", "TreeItem"}
_EXPAND_XAML = {
    "Expander", "SettingsExpander", "ComboBox", "TreeViewItem", "DropDownButton",
    "SplitButton", "MenuFlyoutSubItem", "NavigationViewItem",
}
_VALUE_TYPES = {"Edit", "ComboBox", "Slider", "Spinner"}
_VALUE_XAML = {
    "TextBox", "NumberBox", "PasswordBox", "RichEditBox", "ComboBox", "Slider",
    "AutoSuggestBox", "RatingControl",
}


def _pattern(element, name: str):
    from pywinauto import uia_defines

    return uia_defines.get_elem_interface(element.element_info.element, name)


def read_state(element, control_type: Optional[str], xaml_type: Optional[str]) -> Dict[str, Any]:
    """Read TogglePattern / SelectionItem / ExpandCollapse / Value state.

    Without this, answering "is this toggle on" costs a screenshot of the whole
    window. Probes are gated by control type so a TextBlock is never asked for
    its toggle state; anything a gate misses simply reports no state, which is
    the same as before this existed.
    """
    state: Dict[str, Any] = {}

    def wants(types, xamls) -> bool:
        return control_type in types or xaml_type in xamls

    if wants(_TOGGLE_TYPES, _TOGGLE_XAML):
        try:
            raw = _pattern(element, "Toggle").CurrentToggleState
            state["toggle_state"] = _TOGGLE_STATES.get(raw, raw)
        except Exception:
            pass

    if wants(_SELECT_TYPES, _SELECT_XAML):
        try:
            state["selected"] = bool(_pattern(element, "SelectionItem").CurrentIsSelected)
        except Exception:
            pass

    if wants(_EXPAND_TYPES, _EXPAND_XAML):
        try:
            raw = _pattern(element, "ExpandCollapse").CurrentExpandCollapseState
            state["expanded"] = _EXPAND_STATES.get(raw, raw)
        except Exception:
            pass

    if wants(_VALUE_TYPES, _VALUE_XAML):
        try:
            value = _pattern(element, "Value").CurrentValue
            if value is not None:
                state["value"] = value
        except Exception:
            pass

    return state


# Scroll viewports are found by type rather than by probing: asking each of a
# couple of hundred descendants for a ScrollPattern is a couple of hundred COM
# calls, while a XAML tree holds a handful of scrollers and they are all one of
# these. They are collected in the same pass as the controls themselves, so the
# clip test costs a few property reads rather than a second walk.
_SCROLLER_XAML = {"ScrollViewer", "ScrollView", "ScrollContentPresenter"}
_SCROLLER_TYPES = {"List", "Tree", "DataGrid"}

# A rect in DIPs is a physical rect divided by the scale and rounded, so "flush
# with the edge" has to mean "within a DIP of it".
_CLIP_TOLERANCE = 1


def _viewport_of(element) -> Optional[Dict[str, Any]]:
    """A scrollable element as a viewport: its rect, and where it can still go.

    Both halves are needed. "This control's edge is flush with the viewport's"
    is only evidence of clipping when the scroller can still travel that way: a
    list scrolled to the top has its first row flush with the top edge and
    nothing at all is wrong with it, and a card that legitimately fills a
    vertical scroller's width is flush left and right every single time.
    """
    iface = scroll_interface(element)
    if iface is None:
        return None
    rect = _rect_of(element)
    if not rect or rect["width"] <= 0 or rect["height"] <= 0:
        return None
    offsets = scroll_offsets(iface)
    viewport: Dict[str, Any] = {"rect": rect}
    if "vertical_percent" in offsets:
        percent = offsets["vertical_percent"]
        viewport["can_up"] = percent > 0.5
        viewport["can_down"] = percent < 99.5
    if "horizontal_percent" in offsets:
        percent = offsets["horizontal_percent"]
        viewport["can_left"] = percent > 0.5
        viewport["can_right"] = percent < 99.5
    if len(viewport) == 1:
        return None
    return viewport


def _clipped(rect: Optional[Dict[str, int]], frame: Optional[Dict[str, int]],
             viewports: List[Dict[str, Any]]) -> bool:
    """Is this rect cut off - by the window, or by a scroll viewport inside it?

    UIA's BoundingRectangle is *already* clipped, so the listing has been telling
    the truth and the truth was unreadable: a 44x44 colour swatch cut to four
    DIPs by a scroll viewport reports `44x4`, which is indistinguishable from a
    deliberately short control unless you happen to know the swatch is square.
    The one case where a screenshot of the control is guaranteed useless was the
    one case nothing flagged.

    Two tiers. The free one is the window's own rect - one lookup for a whole
    listing. The second walks the scroll viewports collected during the same
    pass, and only tests the axis a given viewport can actually scroll on, which
    is what keeps the false positives out.
    """
    if not rect:
        return False
    tol = _CLIP_TOLERANCE

    if frame and (
        rect["left"] < frame["left"] - tol
        or rect["top"] < frame["top"] - tol
        or rect["right"] > frame["right"] + tol
        or rect["bottom"] > frame["bottom"] + tol
    ):
        return True

    for viewport in viewports:
        outer = viewport["rect"]
        # Not inside this viewport at all.
        if (
            rect["right"] <= outer["left"] or rect["left"] >= outer["right"]
            or rect["bottom"] <= outer["top"] or rect["top"] >= outer["bottom"]
        ):
            continue
        # The scroller itself, or something wrapping it. Its edges are flush with
        # the viewport's by definition and it is not being clipped by it.
        if (
            rect["left"] <= outer["left"] and rect["top"] <= outer["top"]
            and rect["right"] >= outer["right"] and rect["bottom"] >= outer["bottom"]
        ):
            continue
        if viewport.get("can_up") and rect["top"] <= outer["top"] + tol:
            return True
        if viewport.get("can_down") and rect["bottom"] >= outer["bottom"] - tol:
            return True
        if viewport.get("can_left") and rect["left"] <= outer["left"] + tol:
            return True
        if viewport.get("can_right") and rect["right"] >= outer["right"] - tol:
            return True
    return False


def _desktop():
    from pywinauto import Desktop

    return Desktop(backend="uia")


def get_window(hwnd: int):
    try:
        return _desktop().window(handle=hwnd)
    except Exception as exc:
        raise UiaError(f"cannot bind to window {hwnd}: {exc}") from exc


def _rect_of(element) -> Optional[Dict[str, int]]:
    try:
        r = element.rectangle()
        return {
            "left": r.left, "top": r.top, "right": r.right, "bottom": r.bottom,
            "width": r.width(), "height": r.height(),
        }
    except Exception:
        return None


def describe(
    element,
    control_id: str,
    include_state: bool = True,
    dpi_scale: Optional[float] = None,
) -> Dict[str, Any]:
    info = getattr(element, "element_info", None)
    try:
        name = element.window_text()
    except Exception:
        name = ""
    class_name = getattr(info, "class_name", None)
    control_type = getattr(info, "control_type", None)
    xaml_type = xaml_type_of(class_name)
    rect = _rect_of(element)

    described = {
        "id": control_id,
        "name": name,
        "control_type": control_type,
        # class_name stays raw; xaml_type is the normalized view beside it, so
        # no information is lost.
        "xaml_type": xaml_type,
        "automation_id": getattr(info, "automation_id", None),
        "class_name": class_name,
        "rect": rect,
        "enabled": _safe(element.is_enabled),
        "visible": _safe(element.is_visible),
    }

    if dpi_scale and rect:
        described["rect_dips"] = _to_dips(rect, dpi_scale)
    if include_state:
        described.update(read_state(element, control_type, xaml_type))
    return described


def _to_dips(rect: Dict[str, int], scale: float) -> Dict[str, int]:
    """Convert a physical-pixel rect to effective pixels (DIPs).

    XAML thresholds - AdaptiveTrigger.MinWindowWidth and friends - are written in
    DIPs, so comparing them against physical pixels is how you conclude a
    breakpoint fired when it did not.
    """
    return {k: round(v / scale) for k, v in rect.items()}


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def resolve_scope(hwnd: int, scope: str, controls: List[Dict[str, Any]]):
    """Resolve a `scope` string to the one element to enumerate under.

    Three forms, in the order a caller reaches for them:

      * `c12`            - a control id from the current listing
      * `aid:ContentRoot` - AutomationId, exact
      * `name:Settings`   - UIA name, exact

    Ambiguity fails rather than picking the first match. A repeated AutomationId
    inside an item template is normal, and silently scoping to whichever copy the
    walk reached first would produce a listing that is *plausible* and wrong -
    the failure mode this whole cut exists to remove.
    """
    if scope.startswith("aid:") or scope.startswith("name:"):
        key, _, wanted = scope.partition(":")
        attribute = "automation_id" if key == "aid" else "name"
        window = get_window(hwnd)
        matches = []
        for element in window.descendants():
            info = getattr(element, "element_info", None)
            actual = (
                getattr(info, "automation_id", None) if attribute == "automation_id"
                else _safe(element.window_text, "")
            )
            if actual == wanted:
                matches.append(element)
        if not matches:
            raise UiaError(
                f"no element with {attribute} {wanted!r}; scope by a control id "
                "from the current listing instead"
            )
        if len(matches) > 1:
            raise UiaError(
                f"{len(matches)} elements have {attribute} {wanted!r}; scope by a "
                "control id from the current listing instead"
            )
        return matches[0]

    element, _described = resolve_control(hwnd, scope, controls)
    return element


def _matches_filters(
    control_type: Optional[str],
    xaml_type: Optional[str],
    want_control_type: Optional[str],
    want_xaml_type: Optional[str],
) -> bool:
    """Type filters, applied before `describe` so filtered-out controls cost nothing.

    Case-insensitive: an agent writing `xaml_type="expander"` means the same
    thing as `Expander`, and failing that on case would just cost a retry.
    """
    if want_control_type and (control_type or "").casefold() != want_control_type.casefold():
        return False
    if want_xaml_type and (xaml_type or "").casefold() != want_xaml_type.casefold():
        return False
    return True


def list_controls(
    hwnd: int,
    actionable_only: bool = True,
    include_unnamed: bool = False,
    include_state: bool = True,
    max_controls: int = 300,
    root=None,
    xaml_type: Optional[str] = None,
    control_type: Optional[str] = None,
    name_contains: Optional[str] = None,
) -> Dict[str, Any]:
    """Enumerate a window's controls, a fresh snapshot each call.

    `include_state` defaults on. Measured on the Gallery window: 0.21s without
    state, 0.44s with it (2.09x). That exceeds a "do not more than double" bar for
    defaulting this on, marginally - kept on
    anyway, because the ratio is misleading. The absolute cost is +0.23s, while
    the alternative it removes is a 1400px screenshot: an extra round trip and
    roughly two orders of magnitude more tokens. Gating (see read_state) already
    took the cost down from 2.42x; the rest is COM property reads on the 21 of 36
    controls that genuinely carry state, and only UIA cache requests would remove
    those. Pass include_state=False when only geometry is needed.

    Returns a payload, not a bare list, so truncation is visible: a caller that
    receives 300 controls must be able to tell "300 exist" from "there were 900
    and these are the first third", because the dropped ones are latest in tree
    order - on a scrolled page, exactly the content being looked for.

    Ids are positional (`c0`, `c1`, ...) and carry two traps:

      * they expire when the UI changes shape, and
      * they shift with `actionable_only` / `include_unnamed`, because the
        counter only advances for controls that survive the filter. An id copied
        between two listings taken with different flags addresses a different
        control.

    So an id is only valid against the listing it came from.

    `root` (from `resolve_scope`) enumerates one subtree instead of the window.
    On a NavigationView app the pane's 20-odd items are ahead of everything on
    the page and are re-sent by every unscoped listing, whether or not they are
    the subject; scoping to the content frame drops them outright.

    `xaml_type` / `control_type` / `name_contains` filter server-side, before ids
    are assigned. That ordering is deliberate: filtering after numbering would
    hand back ids with holes in them, and filtering in the caller's context is
    the cost this is meant to avoid. It also means a filtered listing's ids are
    its own - the same trap the flags already carry.
    """
    window = get_window(hwnd)
    try:
        descendants = (root or window).descendants()
    except Exception as exc:
        raise UiaError(f"cannot enumerate controls of {hwnd}: {exc}") from exc

    from .win32.desktop import dpi_scale_for_window

    scale = dpi_scale_for_window(hwnd)
    wanted_name = name_contains.casefold() if name_contains else None

    matched: List[Dict[str, Any]] = []
    viewports: List[Dict[str, Any]] = []
    for element in descendants:
        info = getattr(element, "element_info", None)
        ctype = getattr(info, "control_type", None)
        xtype = xaml_type_of(getattr(info, "class_name", None))

        # A scroll viewport is almost never an actionable control, so it would
        # be skipped by the filter below - but the clip test needs it. Collected
        # here so the whole thing costs a few property reads on a handful of
        # candidates instead of a second walk over the tree.
        if ctype in _SCROLLER_TYPES or xtype in _SCROLLER_XAML:
            viewport = _viewport_of(element)
            if viewport:
                viewports.append(viewport)

        if actionable_only and ctype not in ACTIONABLE_TYPES:
            continue
        if not _matches_filters(ctype, xtype, control_type, xaml_type):
            continue

        described = describe(
            element, f"c{len(matched)}", include_state=include_state, dpi_scale=scale
        )
        if not include_unnamed and not described["name"] and not described["automation_id"]:
            continue
        if wanted_name and wanted_name not in (described["name"] or "").casefold():
            continue
        if is_caption_button(described):
            described["caption_button"] = True
        rect = described["rect"]
        if rect and (rect["width"] <= 0 or rect["height"] <= 0):
            continue
        # A XAML ListItem/Button carries a Text child with the same label sitting
        # inside it. Both are enumerated, which doubles the set-of-mark boxes and
        # gives the agent two ids for one thing. Keep the actionable ancestor.
        if ctype == "Text" and _contained_in_actionable(rect, matched):
            continue
        matched.append(described)

    # Clipping is arithmetic on rects that are already in hand, so it runs after
    # the walk rather than inside it.
    from .win32.desktop import window_rect

    frame = _safe(lambda: window_rect(hwnd))
    for described in matched:
        if _clipped(described.get("rect"), frame, viewports):
            described["clipped"] = True

    # Filtering runs to completion rather than breaking at the cap, so
    # total_matched is exact instead of "at least this many".
    controls = matched[:max_controls]
    payload = {
        "controls": controls,
        "count": len(controls),
        "total_matched": len(matched),
        "total_descendants": len(descendants),
        "truncated": len(matched) > len(controls),
        "dpi_scale": scale,
    }
    # Echo the filters back. An empty listing is otherwise indistinguishable from
    # "the page has nothing on it", when the real answer is usually a filter that
    # matched nothing.
    filters = {
        key: value for key, value in (
            ("xaml_type", xaml_type),
            ("control_type", control_type),
            ("name_contains", name_contains),
        ) if value
    }
    if filters:
        payload["filters"] = filters
    return payload


def is_caption_button(described: Dict[str, Any]) -> bool:
    """Is this one of the window frame's own Minimize/Maximize/Restore/Close buttons?

    Matching on the name alone is wrong, and measurably so: a ComboBox's
    light-dismiss overlay is a Button named "Close" (automation_id
    "Light Dismiss", class ComboBoxLightDismiss), and so is the Close button in
    any ordinary dialog. Flagging those made `click_control` refuse to press
    them with "invoking it would hide or close the window", which is both untrue
    and unhelpful - the caller then has no way to dismiss a popup or close a
    dialog.

    The discriminator is that the real frame buttons are not XAML elements at
    all: they carry no class_name (and an automation_id equal to the caption
    verb). Every in-app button has one - "Button", "ComboBoxLightDismiss",
    "WindowsForms10.BUTTON...". Keying on the absent class_name rather than on
    the automation_id keeps the guard conservative in the direction that matters:
    a frame button that somehow lacked its automation_id is still caught, so the
    tool never invokes a real Close by accident.
    """
    return described.get("name") in CAPTION_CONTROLS and not described.get("class_name")


def scroll_interface(element):
    """The element's ScrollPattern, or None when it does not expose one."""
    try:
        return _pattern(element, "Scroll")
    except Exception:
        return None


def scroll_item_interface(element):
    """The element's ScrollItemPattern, or None.

    This is the pattern that answers "bring this into view" for something that is
    not selectable. `select_control` was the only route before, and it only works
    on selectable items - an Expander or a Group is neither, which left guessing
    a scroll amount and taking a screenshot to find out where it landed.
    """
    try:
        return _pattern(element, "ScrollItem")
    except Exception:
        return None


# UIA reports this percent when the axis does not scroll at all. It is not 0,
# and reading it as 0 would say "at the top" about a thing with no top.
NO_SCROLL = -1


def scroll_offsets(iface) -> Dict[str, Any]:
    """Where a ScrollPattern currently sits.

    Without this, "did that scroll go far enough" costs a screenshot. UIA gives
    percentages and a view size rather than pixels: `view_size` is the share of
    the content visible, so 25 means the viewport shows a quarter of it.
    """
    offsets: Dict[str, Any] = {}
    for axis, percent_property, view_property, scrollable_property in (
        ("vertical", "CurrentVerticalScrollPercent", "CurrentVerticalViewSize",
         "CurrentVerticallyScrollable"),
        ("horizontal", "CurrentHorizontalScrollPercent", "CurrentHorizontalViewSize",
         "CurrentHorizontallyScrollable"),
    ):
        try:
            scrollable = bool(getattr(iface, scrollable_property))
        except Exception:
            continue
        if not scrollable:
            continue
        try:
            percent = round(float(getattr(iface, percent_property)), 1)
            view = round(float(getattr(iface, view_property)), 1)
        except Exception:
            continue
        if percent == NO_SCROLL:
            continue
        offsets[f"{axis}_percent"] = percent
        offsets[f"{axis}_view_size"] = view
        if axis == "vertical":
            offsets["at_top"] = percent <= 0.5
            offsets["at_bottom"] = percent >= 99.5
    return offsets


def _scrolls_along(element, vertical: bool) -> bool:
    iface = scroll_interface(element)
    if iface is None:
        return False
    try:
        return bool(
            iface.CurrentVerticallyScrollable if vertical
            else iface.CurrentHorizontallyScrollable
        )
    except Exception:
        # The pattern is there but the axis flag would not read; let the actual
        # scroll attempt be the judge rather than ruling the element out here.
        return True


def find_scrollable(element, vertical: bool = True, max_scan: int = 200):
    """Find the nearest element that can actually scroll, starting from `element`.

    Callers address the thing they can see - a NavigationView, a page root, a
    list - but ScrollPattern lives on the ScrollViewer buried inside it, or on a
    container above it. Making the caller locate that themselves is busywork:
    they have no view of the visual tree, and the enclosing scroller is not in
    the actionable listing at all.

    Searches self, then descendants breadth-first, then ancestors. Returns
    `(element, where)` where `where` is "self", "descendant" or "ancestor", or
    `(None, None)` when nothing in reach scrolls on that axis.
    """
    if _scrolls_along(element, vertical):
        return element, "self"

    queue = [element]
    scanned = 0
    while queue and scanned < max_scan:
        current = queue.pop(0)
        try:
            children = current.children()
        except Exception:
            continue
        for child in children:
            scanned += 1
            if scanned > max_scan:
                break
            if _scrolls_along(child, vertical):
                return child, "descendant"
            queue.append(child)

    node = element
    for _ in range(6):
        try:
            node = node.parent()
        except Exception:
            break
        if node is None:
            break
        if _scrolls_along(node, vertical):
            return node, "ancestor"

    return None, None


def _contained_in_actionable(rect: Optional[Dict[str, int]], existing: List[Dict[str, Any]]) -> bool:
    if not rect:
        return False
    for other in existing:
        if other["control_type"] in ("Text", "Document"):
            continue
        outer = other.get("rect")
        if not outer:
            continue
        if (
            rect["left"] >= outer["left"]
            and rect["top"] >= outer["top"]
            and rect["right"] <= outer["right"]
            and rect["bottom"] <= outer["bottom"]
        ):
            return True
    return False


def resolve_control(hwnd: int, control_id: str, controls: List[Dict[str, Any]]):
    """Map a snapshot id back to a live UIA element.

    Re-walks the tree and matches on the recorded identity rather than trusting
    the index, so a control that moved still resolves and one that vanished
    fails loudly instead of acting on whatever took its place.
    """
    target = next((c for c in controls if c["id"] == control_id), None)
    if target is None:
        raise UiaError(
            f"unknown control id {control_id!r}; "
            f"available: {[c['id'] for c in controls][:20]}"
        )

    window = get_window(hwnd)
    for element in window.descendants():
        info = getattr(element, "element_info", None)
        if (
            _safe(element.window_text, "") == target["name"]
            and getattr(info, "control_type", None) == target["control_type"]
            and getattr(info, "automation_id", None) == target["automation_id"]
        ):
            return element, target

    raise UiaError(
        f"control {control_id!r} ({target['name']!r}) is no longer present; "
        "re-read the control list"
    )


def _identity(element) -> tuple:
    """The triple `resolve_control` matches on, for batch lookups."""
    info = getattr(element, "element_info", None)
    return (
        _safe(element.window_text, ""),
        getattr(info, "control_type", None),
        getattr(info, "automation_id", None),
    )


def refresh_controls(
    hwnd: int,
    ids: List[str],
    controls: List[Dict[str, Any]],
    include_state: bool = True,
) -> Dict[str, Any]:
    """Re-read specific rows of the last listing, in one tree walk.

    The positional-id design forces a full re-listing after anything that changes
    the tree's shape, because an id cannot be trusted afterwards. That is the
    right default and it is also why a session ends up re-sending the same 40
    rows to answer "did that one card move". This answers exactly that, at the
    cost of the walk and nothing else: each id comes back as a live row or as
    `stale`, which is the fact that actually breaks the re-listing cycle.

    Identity matching is `resolve_control`'s, first match wins, so a duplicated
    identity resolves the same way both paths do.
    """
    known = {c["id"]: c for c in controls}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise UiaError(
            f"unknown control id(s) {unknown}; they must come from the current "
            f"listing, which has {len(known)} rows"
        )

    from .win32.desktop import dpi_scale_for_window

    scale = dpi_scale_for_window(hwnd)
    window = get_window(hwnd)
    try:
        descendants = window.descendants()
    except Exception as exc:
        raise UiaError(f"cannot enumerate controls of {hwnd}: {exc}") from exc

    index: Dict[tuple, Any] = {}
    for element in descendants:
        index.setdefault(_identity(element), element)

    rows: List[Dict[str, Any]] = []
    for control_id in ids:
        target = known[control_id]
        element = index.get(
            (target["name"], target["control_type"], target["automation_id"])
        )
        if element is None:
            rows.append({
                "id": control_id,
                "stale": True,
                "was": target.get("name")
                or target.get("automation_id")
                or target.get("xaml_type")
                or target.get("control_type"),
            })
            continue
        rows.append(
            describe(element, control_id, include_state=include_state, dpi_scale=scale)
        )

    return {"hwnd": hwnd, "rows": rows, "dpi_scale": scale}


# Layout answers are given in DIPs, which are physical pixels divided by the
# scale and rounded. Two elements that a designer laid out identically can
# therefore differ by one after rounding, so "aligned" means within a DIP. The
# exact delta is reported beside the verdict, so nothing is hidden by this.
_ALIGN_TOLERANCE = 1


def measure(
    hwnd: int,
    control_ids: List[str],
    controls: List[Dict[str, Any]],
    units: str = "dips",
    max_ids: int = 8,
) -> Dict[str, Any]:
    """Rects and pairwise deltas for a handful of controls, and nothing else.

    "Is this card the same height and x as that one" is the layout-parity
    question, and answering it from two full listings means diffing thousands of
    tokens of rows that were never in question. The deltas are computed here
    because they are arithmetic, not judgement - and because doing it in the
    caller's context is how a rounding error becomes a wrong conclusion.

    Rects are re-read rather than taken from the cached listing: a measurement of
    where a control used to be is worse than no measurement.
    """
    if not control_ids:
        raise UiaError("measure needs at least one control id")
    if len(control_ids) > max_ids:
        raise UiaError(
            f"measure takes at most {max_ids} ids at once, got {len(control_ids)}; "
            "it is for comparing a few controls, not for listing a page"
        )

    live = refresh_controls(hwnd, control_ids, controls, include_state=False)
    scale = live["dpi_scale"]
    rect_key = "rect" if units == "px" else "rect_dips"

    rows: List[Dict[str, Any]] = []
    stale: List[str] = []
    for entry in live["rows"]:
        if entry.get("stale"):
            stale.append(entry["id"])
            rows.append(entry)
            continue
        rows.append({
            "id": entry["id"],
            "name": entry.get("name"),
            "rect": entry.get(rect_key) or entry.get("rect"),
        })

    measurable = [r for r in rows if r.get("rect")]
    pairs: List[Dict[str, Any]] = []
    for i, first in enumerate(measurable):
        for second in measurable[i + 1:]:
            pairs.append(_pair_delta(first, second))

    payload: Dict[str, Any] = {
        "hwnd": hwnd,
        "controls": rows,
        "pairs": pairs,
        "dpi_scale": scale,
        "units": units,
    }
    if stale:
        payload["stale"] = stale
    return payload


def _pair_delta(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    ra, rb = a["rect"], b["rect"]
    dx = rb["left"] - ra["left"]
    dy = rb["top"] - ra["top"]
    dw = rb["width"] - ra["width"]
    dh = rb["height"] - ra["height"]

    def near(value: int) -> bool:
        return abs(value) <= _ALIGN_TOLERANCE

    notes: List[str] = []
    if near(dx):
        notes.append("left aligned")
    if near(dy):
        notes.append("top aligned")
    if near(dw):
        notes.append("same width")
    if near(dh):
        notes.append("same height")
    center_delta = (rb["left"] + rb["width"] / 2) - (ra["left"] + ra["width"] / 2)
    if near(round(center_delta)) and not near(dx):
        notes.append("centred on the same x")

    # Gap, which is what "are these two cards spaced consistently" comes down to.
    if rb["top"] >= ra["bottom"]:
        notes.append(f"gap {rb['top'] - ra['bottom']} below")
    elif ra["top"] >= rb["bottom"]:
        notes.append(f"gap {ra['top'] - rb['bottom']} above")
    elif rb["left"] >= ra["right"]:
        notes.append(f"gap {rb['left'] - ra['right']} right")
    elif ra["left"] >= rb["right"]:
        notes.append(f"gap {ra['left'] - rb['right']} left")
    else:
        notes.append("overlapping")

    return {
        "a": a["id"], "b": b["id"],
        "dx": dx, "dy": dy, "dw": dw, "dh": dh,
        "notes": notes,
    }


def audit_accessibility(hwnd: int) -> Dict[str, Any]:
    """Find actionable controls with neither a UIA name nor an AutomationId.

    A WinUI Button whose content is a StackPanel of icon + text derives no name,
    so it disappears from the default listing. That reads as an automation
    problem first, but it is an accessibility bug underneath - a screen reader
    hits the same wall, and the fix is the same `AutomationProperties.Name`.
    """
    window = get_window(hwnd)
    try:
        descendants = window.descendants()
    except Exception as exc:
        raise UiaError(f"cannot enumerate controls of {hwnd}: {exc}") from exc

    from .win32.desktop import dpi_scale_for_window

    scale = dpi_scale_for_window(hwnd)
    findings: List[Dict[str, Any]] = []

    for element in descendants:
        info = getattr(element, "element_info", None)
        ctype = getattr(info, "control_type", None)
        if ctype not in ACTIONABLE_TYPES:
            continue
        # Text and Document carry their content as the name; a nameless one is
        # an empty label, not a control a user is expected to reach.
        if ctype in ("Text", "Document"):
            continue
        if _safe(element.window_text, "") or getattr(info, "automation_id", None):
            continue
        rect = _rect_of(element)
        if not rect or rect["width"] <= 0 or rect["height"] <= 0:
            continue
        findings.append({
            "control_type": ctype,
            "xaml_type": xaml_type_of(getattr(info, "class_name", None)),
            "class_name": getattr(info, "class_name", None),
            "rect": rect,
            "rect_dips": _to_dips(rect, scale) if scale else None,
            "fix": "set AutomationProperties.Name on this element",
        })

    return {
        "hwnd": hwnd,
        "unnamed_actionable": findings,
        "count": len(findings),
        "total_descendants": len(descendants),
        "dpi_scale": scale,
    }


def control_tree(hwnd: int, max_depth: int = 6, max_nodes: int = 400) -> Dict[str, Any]:
    """Nested structural view, for when flat lists lose the layout relationships."""
    window = get_window(hwnd)
    counter = {"n": 0}

    def walk(element, depth: int) -> Optional[Dict[str, Any]]:
        if depth > max_depth or counter["n"] >= max_nodes:
            return None
        counter["n"] += 1
        info = getattr(element, "element_info", None)
        node = {
            "name": _safe(element.window_text, ""),
            "control_type": getattr(info, "control_type", None),
            "automation_id": getattr(info, "automation_id", None),
            "rect": _rect_of(element),
            "children": [],
        }
        try:
            for child in element.children():
                sub = walk(child, depth + 1)
                if sub:
                    node["children"].append(sub)
        except Exception:
            pass
        return node

    return walk(window, 0) or {}
