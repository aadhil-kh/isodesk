"""isodesk MCP server.

Exposes isolated Windows desktop sessions as MCP tools, so any harness - Claude
Code, or anything else speaking MCP - drives a GUI app directly. There is no
HostAgent, no AppAgent, and no LLM in this process: the calling model *is* the
agent, and these tools are its hands and eyes.

Run with:
    python -m isodesk.mcp_server            # stdio transport
"""

from __future__ import annotations

import atexit
import base64
from typing import Annotated, Any, Dict, List, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from fastmcp.utilities.types import Image
from pydantic import Field

from . import render
from .session import SessionError, SessionManager
from .win32 import process as pr

mcp = FastMCP(
    name="isodesk",
    instructions=(
        "Isolated Windows desktop sessions. Each session is a separate Win32 "
        "desktop object with its own foreground window, focus and cursor, so "
        "automating an app there never disturbs the user's own desktop.\n\n"
        "Typical loop: session_create -> session_launch_app or "
        "session_activate_app -> get_controls (semantic) or screenshot "
        "(visual) -> click_control / set_text -> screenshot to verify -> "
        "session_destroy.\n\n"
        "Ask the cheapest question that answers what you need, in this order: "
        "measure (geometry of two controls) < refresh_controls (a few ids) < "
        "get_controls with scope= or xaml_type= < a full get_controls < "
        "screenshot(control_id=) < a full screenshot.\n\n"
        "IMPORTANT - every action goes through UI Automation, because a "
        "background desktop does not own the input queue and therefore cannot "
        "receive synthetic keyboard or mouse events. In practice:\n"
        "  * To press a button, use click_control (not a click at coordinates).\n"
        "  * To enter text, use set_text. There is NO way to type keystrokes and "
        "no way to send keyboard shortcuts such as Ctrl+S; reach the same "
        "command through its menu or toolbar control instead.\n"
        "  * click_xy resolves the UIA element under the point and invokes its "
        "pattern, so it lands on controls, not pixels - and it may resolve to an "
        "ancestor or a sibling of what is visually there, which the response "
        "reports as `retargeted`. SendInput is not used and cannot be. Use it "
        "only when get_controls does not surface the target.\n"
        "  * Drag operations are not available.\n\n"
        "Prefer control ids from get_controls over coordinates throughout: they "
        "need neither focus nor compositing and are reliable on WinUI 3, WPF and "
        "WinForms."
    ),
)

manager = SessionManager()

# Parameters that repeat across nearly every tool. Declaring them once keeps the
# description on all thirty-odd copies identical, and keeps every one of them a
# plain `integer` / `string` in the emitted schema rather than an `anyOf`.
Hwnd = Annotated[int, Field(
    description="Window to act on. 0, the default, means the session's active "
                "window; pass a popup's hwnd from list_windows to reach a "
                "flyout, menu or drop-down.",
)]
Listing = Annotated[str, Field(
    description="The listing token the ids came from. Quote it and an id from a "
                "superseded listing is refused instead of resolved against "
                "whatever now holds that position. Empty skips the check.",
    examples=["L4"],
)]
ControlId = Annotated[str, Field(
    description="A control id from the current listing.",
    examples=["c14"],
)]


@atexit.register
def _cleanup() -> None:
    """Never leave orphaned desktops or app processes behind."""
    try:
        manager.destroy_all()
    except Exception:
        pass


def _call(session_id: str, method: str, /, **params) -> Dict[str, Any]:
    # Positional-only for the reason Session.call is: click_xy has a parameter
    # called `method`, and a plain `method` here collided with it on every call.
    try:
        return manager.get(session_id).call(method, **params)
    except SessionError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        raise ToolError(f"{method} failed: {exc}") from exc


def _opt(value):
    """Sentinel -> None, at the boundary between the schema and the worker.

    Nullable unions are kept out of the public schema: pydantic renders
    `Optional[X]` as an `anyOf` with no top-level `"type"`, and a client that
    keys its parameter rendering and its argument coercion off `schema.type`
    then displays nothing and coerces nothing - it passes the value through as
    text, so a *correct* argument fails with a type error exactly as a guessed
    one would. Every such parameter therefore takes a concrete type and a
    sentinel default, and the sentinel is normalized back here, where the worker
    already reads `None` as "use the default".
    """
    return value if value not in (0, "", [], None) else None


def _text_result(text: str) -> ToolResult:
    """Return a rendered listing as text.

    A `-> Dict[str, Any]` annotation makes FastMCP generate an outputSchema,
    which then rejects anything but structured output - the same constraint
    `_image_result` documents below. Control listings are tables, and a table
    costs about half as much as the JSON of the same table, so they go over as
    text and the tool's return type stays unannotated.
    """
    return ToolResult(content=[text])


def _render(renderer, payload: Dict[str, Any], units: str, fmt: str) -> str:
    """Render a worker payload, turning a bad units/format into a ToolError."""
    try:
        return renderer(payload, units=units, fmt=fmt)
    except render.RenderError as exc:
        raise ToolError(str(exc)) from exc


def _crop_lines(payload: Dict[str, Any]) -> List[str]:
    """The crop echo, with every frame named.

    Three different rectangles are in play, and collapsing them into one
    unlabelled line is what made the input frame and the echo look like a
    contradiction:

      * what the caller asked for - screen-absolute, in the caller's units, the
        same frame a control listing prints its rects in;
      * what was actually cut out of the capture - window-relative pixels, after
        clamping to the window;
      * what was delivered - pixels again, after the `max_width` downscale,
        which happens after the crop and so changes the numbers a third time.

    Printing only the middle one and calling it "the crop" left the caller to
    infer two frame changes and a resize that were never stated.
    """
    crop = payload.get("crop")
    if not crop:
        return []

    delivered = payload.get("delivered") or {}
    requested = payload.get("crop_requested") or {}
    shrunk = (
        delivered
        and (delivered.get("width"), delivered.get("height"))
        != (crop["width"], crop["height"])
    )
    if shrunk:
        head = (
            f"cropped to {delivered['width']}x{delivered['height']} px delivered "
            f"(downscaled {delivered.get('scale')}x from "
            f"{crop['width']}x{crop['height']})"
        )
    else:
        head = f"cropped to {crop['width']}x{crop['height']} px delivered"

    lines = [head]
    if requested:
        lines.append(
            f"  requested: {requested['left']},{requested['top']} "
            f"{requested['width']}x{requested['height']} "
            f"{requested.get('units', 'px')} (screen-absolute, same frame as the "
            "rects in a control listing)"
        )
    lines.append(
        f"  in-window: {crop['left']},{crop['top']} "
        f"{crop['width']}x{crop['height']} px"
    )
    if payload.get("clamped"):
        lines.append(f"  {payload['clamped']}")
    return lines


def _image_result(payload: Dict[str, Any], note: str = "") -> ToolResult:
    """Return the screenshot as a real MCP image plus a compact text summary.

    ToolResult is used rather than returning a plain list: an annotated return
    type makes FastMCP generate an outputSchema, which then rejects mixed
    image+text content with "outputSchema defined but no structured output
    returned". ToolResult is the supported way to emit content blocks directly.
    """
    image = Image(data=base64.b64decode(payload["image_base64"]), format="png")
    quality = payload.get("quality", {})
    lines = [
        f"window: {payload.get('title')!r} (hwnd {payload.get('hwnd')})",
        f"rect: {payload.get('rect')}",
    ]
    if not quality.get("looks_rendered", True):
        lines.append(
            "WARNING: this frame looks blank "
            f"(non-black {quality.get('non_black_ratio')}, "
            f"{quality.get('distinct_colors')} colors). The window may be "
            "minimized or still starting."
        )

    scrolled = payload.get("scrolled_into_view")
    if scrolled and scrolled.get("error"):
        lines.append(
            f"scroll_into_view could not run ({scrolled['error']}); the control "
            "was captured where it stood."
        )
    elif scrolled and scrolled.get("moved"):
        lines.append(
            "scrolled_into_view: brought into view before capture via "
            f"{scrolled.get('method')}, so the rest of this frame scrolled with it. "
            f"rect {scrolled.get('rect_before')} -> {scrolled.get('rect_after')}. "
            "Pass scroll_into_view=false to capture it where it stood."
        )

    lines.extend(_crop_lines(payload))
    if payload.get("crop_hint"):
        lines.append(payload["crop_hint"])

    if payload.get("controls"):
        lines.append(
            f"{len(payload['controls'])} controls annotated; ids are the box "
            f"labels (listing {payload.get('listing')})."
        )
    for popup in payload.get("popups", []):
        rect = popup["rect_dips"]
        lines.append(
            f"POPUP OPEN over this window: hwnd {popup['hwnd']} "
            f"({rect['width']}x{rect['height']} dips). This flyout, menu, drop-down "
            "or tooltip is hosted in its own top-level window, so none of its "
            f"content is in this image. screenshot(hwnd={popup['hwnd']}) to see it, "
            f"get_controls(hwnd={popup['hwnd']}) to act on its items."
        )
    if note:
        lines.append(note)
    return ToolResult(content=[image.to_image_content(), "\n".join(lines)])


# ---------------------------------------------------------------------------
# session lifecycle
# ---------------------------------------------------------------------------


@mcp.tool
def session_create(
    label: Annotated[str, Field(description="Optional human-readable label.")] = "",
) -> Dict[str, Any]:
    """Create a new isolated Windows desktop session.

    Returns the session_id needed by every other tool. Takes about a second.
    Always call session_destroy when finished, or the desktop and its apps leak.
    """
    try:
        return manager.create(label=label).info()
    except SessionError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
def session_list(
    include_orphans: Annotated[bool, Field(description="Also report isodesk desktops left behind by a host that died without tearing them down.")] = False,
) -> List[Dict[str, Any]]:
    """List the isolated sessions this server currently owns.

    `include_orphans` also reports desktops from a previous host process that was
    killed rather than exiting: they are invisible, and the app still running on
    one holds its own binaries open, which is felt later as a build or a file
    copy that blocks on a lock for no visible reason. Clear them with
    session_reap.
    """
    sessions = manager.list()
    if include_orphans:
        sessions += manager.orphans()
    return sessions


@mcp.tool
def session_reap(
    session_id: Annotated[str, Field(description="An orphaned session id from session_list(include_orphans=true), or 'all'.")],
) -> List[Dict[str, Any]]:
    """Kill and close orphaned desktops left by a host that never tore them down.

    Only touches desktops no live session owns. A pid that also has a window on
    the user's own desktop is never killed, so a single-instance app that handed
    off to a copy there is left alone.
    """
    try:
        return manager.reap(session_id)
    except SessionError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
def session_destroy(
    session_id: Annotated[str, Field(description="Session to tear down.")],
) -> Dict[str, Any]:
    """Destroy a session: stop its worker, kill its apps, close its desktop.

    Processes that also own a window on the user's own desktop are never killed.
    """
    try:
        return manager.destroy(session_id)
    except SessionError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
def session_launch_app(
    session_id: str,
    command: Annotated[str, Field(description="Command line for an unpackaged exe, e.g. 'notepad.exe' or a full path in quotes.")],
    cwd: Annotated[str, Field(description="Working directory. Empty inherits the worker's.")] = "",
    wait_seconds: Annotated[float, Field(description="How long to wait for a window to appear.")] = 8.0,
) -> Dict[str, Any]:
    """Launch an unpackaged executable inside the session's isolated desktop.

    For Store/MSIX apps use session_activate_app instead - their executables are
    ACL-protected and cannot be launched this way.

    The response includes `module`: the path and mtime of the binary that is
    actually running. Check the mtime against your build before concluding a
    change did not take effect - a stale deploy layout looks exactly like a
    change that did not work.
    """
    return _call(session_id, "launch", cmdline=command, cwd=_opt(cwd), wait=wait_seconds)


@mcp.tool
def session_activate_app(
    session_id: str,
    aumid: Annotated[str, Field(description="App User Model ID. Use find_installed_app to look one up.", examples=["Microsoft.WinUI3ControlsGallery_8wekyb3d8bbwe!App"])],
    arguments: Annotated[str, Field(description="Activation arguments passed to the app.")] = "",
    wait_seconds: Annotated[float, Field(description="How long to wait for a window to appear.")] = 15.0,
) -> Dict[str, Any]:
    """Launch a packaged (MSIX/Store) app inside the session's isolated desktop.

    The response includes `module`, with the running binary's path and mtime plus
    the package's `install_location`. Compare the mtime against your build to
    catch a stale deploy before it is mistaken for a change that did not work.

    WinUI 3 and other desktop MSIX apps open in the session. UWP apps that use a
    CoreWindow (Calculator, for one) cannot: Windows frames them on the user's
    desktop. That case is refused, and an instance this call started is closed.
    """
    return _call(session_id, "activate", aumid=aumid, arguments=arguments, wait=wait_seconds)


@mcp.tool
def find_installed_app(
    name: Annotated[str, Field(description="Part of the app's display name, e.g. 'Gallery'.")],
) -> List[Dict[str, str]]:
    """Look up the AUMID of an installed packaged app, for session_activate_app."""
    return pr.resolve_aumid(name)


@mcp.tool
def session_run(
    session_id: str,
    command: Annotated[str, Field(description="Shell command to run inside the session.")],
    timeout_seconds: Annotated[float, Field(description="Give up after this many seconds.")] = 60.0,
    cwd: Annotated[str, Field(description="Working directory. Empty inherits the worker's.")] = "",
) -> Dict[str, Any]:
    """Run a console command on the session's desktop and capture its output."""
    return _call(session_id, "run", cmdline=command, timeout=timeout_seconds, cwd=_opt(cwd))


# ---------------------------------------------------------------------------
# observation
# ---------------------------------------------------------------------------


@mcp.tool
def list_windows(session_id: str) -> Dict[str, Any]:
    """List application windows currently open on the session's desktop.

    Entries flagged `popup: true` are flyouts, menus, ComboBox drop-downs and
    tooltips hosted in their own top-level window, which is the common case: such
    a popup is not part of the window it floats over, so it has to be captured
    and enumerated by its own hwnd.

    An empty popup list is not proof that nothing opened. Some WinUI 3 flyouts
    are hosted inside the parent's own content and produce no window here at all.
    The test that works either way costs one call: if no popup is listed,
    `get_controls` on the parent will contain the flyout's items if it opened,
    and will not if it did not.
    """
    return _call(session_id, "list_windows")


@mcp.tool
def screenshot(
    session_id: str,
    hwnd: Hwnd = 0,
    annotated: Annotated[bool, Field(description="Overlay numbered boxes on every actionable control (set-of-mark). Use this when you intend to act next.")] = False,
    max_width: Annotated[int, Field(description="Downscale to this width to save tokens. 1400 is usually plenty; 0 sends the crop at full size.")] = 1400,
    control_id: Annotated[str, Field(description="Crop to this control's rect plus padding, instead of returning the whole window.", examples=["c14"])] = "",
    crop: Annotated[List[int], Field(
        description="Crop to [x, y, width, height] - screen-absolute, the same "
                    "frame as the rects in a control listing. DIPs unless "
                    "units='px'.",
        min_length=4, max_length=4, examples=[[472, 558, 716, 70]],
    )] = [],
    padding: Annotated[int, Field(description="Extra margin around a cropped region, so the control is seen in its context.")] = 8,
    units: Annotated[str, Field(description="Units for crop/padding: 'dips' (default) or 'px'.")] = "dips",
    scroll_into_view: Annotated[bool, Field(description="With control_id, scroll the control into view before capturing. Turn this off only to capture a control deliberately left off-view.")] = True,
    listing: Listing = "",
) -> ToolResult:
    """Capture a window in the isolated session.

    Uses PrintWindow with PW_RENDERFULLCONTENT, the only capture path that works
    for DirectComposition-rendered (WinUI 3) windows on a background desktop.
    A minimized window is restored first, since it would otherwise capture blank.

    Captures ONE window. Most flyouts, menus, drop-downs and tooltips are hosted
    in their own top-level window, so they are absent from the parent's frame and
    the control they belong to just looks closed; any such popup is listed in the
    response with its hwnd, and capturing that hwnd shows it. Some WinUI 3
    flyouts are hosted in-frame instead, with no popup window at all - if nothing
    is listed here and you expected a popup, `get_controls` on this window will
    contain the flyout's items if it opened.

    Reports `dpi_scale` and `rect_dips` beside `rect`. To read control state -
    whether a toggle is on, an item selected, an expander open - use get_controls
    instead; it carries that directly and costs far less than an image.

    **Crop when the question is about one thing.** `control_id` (or an explicit
    `crop`) returns that region plus `padding` rather than the whole frame.
    Inspecting two 70-dip rows in a full 1400px window costs an image budget out
    of all proportion to the question, and the comparison is easier to see
    cropped. The capture itself is unavoidably whole-window - PrintWindow cannot
    do less - so the saving is in what is encoded and sent.

    The crop echo names all three frames it passes through: what you requested
    (screen-absolute, in your units), what was cut from the capture
    (window-relative pixels, after clamping) and what was delivered (after the
    `max_width` downscale). Copying a rect straight out of a listing is correct;
    the echo is simply reported in a different frame from the input.

    `control_id` scrolls the control into view first, because UIA reports the
    *clipped* rect: a control cut to a few pixels by a scroll viewport crops to a
    sliver that shows nothing. When that scroll moves anything, the response says
    so and gives the rect before and after - the rest of the frame has moved too.
    """
    payload = _call(
        session_id, "screenshot", hwnd=_opt(hwnd), annotated=annotated,
        max_width=_opt(max_width), control_id=_opt(control_id), crop=_opt(crop),
        padding=padding, units=units, scroll_into_view=scroll_into_view,
        listing=_opt(listing),
    )
    return _image_result(payload)


@mcp.tool
def get_controls(
    session_id: str,
    hwnd: Hwnd = 0,
    scope: Annotated[str, Field(description="Enumerate one subtree instead of the whole window: a control id from the current listing, 'aid:<AutomationId>' or 'name:<exact name>'.", examples=["c4", "aid:ContentRoot", "name:Settings"])] = "",
    xaml_type: Annotated[str, Field(description="Keep only this XAML type. This is the filter that works on WinUI 3.", examples=["Expander", "ToggleSwitch"])] = "",
    control_type: Annotated[str, Field(description="Keep only this UIA control type.", examples=["ComboBox"])] = "",
    name_contains: Annotated[str, Field(description="Keep only controls whose name contains this, case-insensitive.", examples=["Startup"])] = "",
    actionable_only: Annotated[bool, Field(description="Keep only control types worth acting on. Turn off to see layout containers too.")] = True,
    include_unnamed: Annotated[bool, Field(description="Include controls with neither a name nor an automation id.")] = False,
    include_state: Annotated[bool, Field(description="Include toggle_state / selected / expanded / value. Costs little; leave on unless you only need geometry.")] = True,
    units: Annotated[str, Field(description="'dips' (default), 'px' or 'both'. DIPs are what XAML adaptive thresholds are written in; the two differ only by dpi_scale, so asking for both is usually paying twice for one fact.")] = "dips",
    format: Annotated[str, Field(description="'compact' (default, one line per control) or 'json'.")] = "compact",
) -> ToolResult:
    """Enumerate a window's controls via UI Automation.

    One line per control:

        c23 Expander[Button] "Startup" #StartupCard 472,558 716x70 expanded

    That is: id, `xaml_type[control_type]` (the second only when it differs -
    UIA calls a ToggleSwitch, an Expander and a Button all `Button`), quoted
    name, #AutomationId, `left,top widthxheight`, then state words. Rects are
    DIPs and the scale is stated once in the header.

    **Fields at their default are omitted.** No name, no AutomationId, enabled
    and visible say nothing at all; `disabled`, `hidden`, `on`/`off`,
    `selected`, `expanded`/`collapsed` and `="value"` are always deviations. An
    absent `enabled` means enabled, not unknown - `enabled?` is unknown.
    `format="json"` returns the structured payload, `units="px"`/`"both"` the
    physical rects.

    **`clipped` means the rect you are reading is a cut-down one.** UIA reports
    the clipped BoundingRectangle, so a 44x44 swatch cut to four DIPs by a scroll
    viewport reports `44x4` and looks like a short control. On a `clipped` row,
    do not screenshot it and do not measure it - `scroll_to_control` first, or
    let `screenshot(control_id=)` do it for you.

    This is also where an in-frame flyout shows up: a WinUI 3 Flyout that opened
    without producing a popup window has its items right here, in the parent's
    own listing.

    **Scope and filter instead of re-reading the window.** `scope` enumerates one
    subtree - on a NavigationView app, scoping to the content frame drops the
    pane's 20-odd items that every unscoped listing repeats. `xaml_type` is how
    you ask for "every Expander on this page"; `control_type` cannot express it.

    Ids are positional and belong to the listing that produced them, which the
    header names (`listing L4`). Every action tool takes an optional `listing=`
    and refuses an id from a superseded one instead of resolving it against
    whatever now holds that position. Ids shift with `scope`, the filters,
    `actionable_only` and `include_unnamed`, because the counter only advances
    for controls that survive - so ids never travel between listings.

    To re-read a few rows after acting, use `refresh_controls`, not another full
    listing. To compare positions and sizes, use `measure`.
    """
    payload = _call(
        session_id, "get_controls", hwnd=_opt(hwnd),
        actionable_only=actionable_only, include_unnamed=include_unnamed,
        include_state=include_state, scope=_opt(scope), xaml_type=_opt(xaml_type),
        control_type=_opt(control_type), name_contains=_opt(name_contains),
    )
    return _text_result(_render(render.listing, payload, units, format))


@mcp.tool
def refresh_controls(
    session_id: str,
    ids: Annotated[List[str], Field(description="Control ids from the current listing.", examples=[["c4", "c7"]])],
    hwnd: Hwnd = 0,
    listing: Listing = "",
    units: Annotated[str, Field(description="'dips' (default), 'px' or 'both'.")] = "dips",
    format: Annotated[str, Field(description="'compact' (default) or 'json'.")] = "compact",
) -> ToolResult:
    """Re-read specific controls, instead of re-listing the window.

    Each id comes back as a live row or as `STALE` - meaning that control is no
    longer in the tree, so the whole listing has moved on. This is the cheap
    answer to "did that card move", "is the toggle on now", "did my click take
    effect", all of which otherwise cost a full enumeration because positional
    ids cannot be trusted across a change in the tree's shape.
    """
    payload = _call(
        session_id, "refresh_controls", ids=ids, hwnd=_opt(hwnd),
        listing=_opt(listing),
    )
    return _text_result(_render(render.refreshed, payload, units, format))


@mcp.tool
def measure(
    session_id: str,
    control_ids: Annotated[List[str], Field(description="Two to eight control ids from the current listing.", examples=[["c4", "c7"]])],
    hwnd: Hwnd = 0,
    listing: Listing = "",
    units: Annotated[str, Field(description="'dips' (default) or 'px'.")] = "dips",
    format: Annotated[str, Field(description="'compact' (default) or 'json'.")] = "compact",
) -> ToolResult:
    """Compare the position and size of a few controls, and nothing else.

    Returns each control's rect and every pairwise delta - dx, dy, dw, dh - with
    the verdicts spelled out: left aligned, same width, same height, the gap
    between them, or overlapping. Rects are re-read live, not taken from the
    listing.

    This is the answer to "is this card the same height and x as that one",
    which is otherwise done by diffing two full listings by hand. Alignment is
    judged within one DIP, because a rect in DIPs is a physical rect divided by
    the scale and rounded; the exact delta is printed beside the verdict.
    """
    payload = _call(
        session_id, "measure", control_ids=control_ids, hwnd=_opt(hwnd),
        listing=_opt(listing), units=units,
    )
    return _text_result(_render(render.measurement, payload, units, format))


@mcp.tool
def audit_accessibility(session_id: str, hwnd: Hwnd = 0) -> Dict[str, Any]:
    """Find actionable controls with no UIA name and no AutomationId.

    These are invisible to `get_controls` by default and equally unreachable for
    a screen reader - typically a WinUI Button whose content is a panel of icon
    plus text, which derives no name. The fix is `AutomationProperties.Name`.
    """
    return _call(session_id, "audit_accessibility", hwnd=_opt(hwnd))


@mcp.tool
def get_control_tree(
    session_id: str,
    hwnd: Hwnd = 0,
    max_depth: Annotated[int, Field(description="How deep to walk the XAML/UIA tree.")] = 6,
) -> Dict[str, Any]:
    """Get the nested control tree, when the flat control list loses structure."""
    return _call(session_id, "get_control_tree", hwnd=_opt(hwnd), max_depth=max_depth)


@mcp.tool
def get_text(session_id: str, control_id: ControlId, hwnd: Hwnd = 0,
             listing: Listing = "") -> Dict[str, Any]:
    """Read a control's text or value."""
    return _call(session_id, "get_text", control_id=control_id, hwnd=_opt(hwnd),
                 listing=_opt(listing))


# ---------------------------------------------------------------------------
# actions - UIA first
# ---------------------------------------------------------------------------


@mcp.tool
def click_control(
    session_id: str,
    control_id: ControlId,
    hwnd: Hwnd = 0,
    listing: Listing = "",
) -> Dict[str, Any]:
    """Activate a control the way a click would, via UI Automation.

    Tries Invoke, then SelectionItem, Toggle, ExpandCollapse, and finally
    LegacyIAccessible. Needs no focus and no cursor movement. The response says
    which pattern took effect.
    """
    return _call(session_id, "click_control", control_id=control_id,
                 hwnd=_opt(hwnd), listing=_opt(listing))


@mcp.tool
def set_text(
    session_id: str,
    control_id: ControlId,
    text: Annotated[str, Field(description="The value to set.")],
    hwnd: Hwnd = 0,
    clear: Annotated[bool, Field(description="Replace existing content rather than appending.")] = True,
    listing: Listing = "",
) -> Dict[str, Any]:
    """Set an edit control's text via ValuePattern.

    Preferred over typing: it is atomic, needs no focus, and cannot drop
    characters the way synthesized keystrokes can.
    """
    return _call(session_id, "set_text", control_id=control_id, text=text,
                 hwnd=_opt(hwnd), clear=clear, listing=_opt(listing))


@mcp.tool
def toggle_control(session_id: str, control_id: ControlId, hwnd: Hwnd = 0,
                   listing: Listing = "") -> Dict[str, Any]:
    """Toggle a checkbox or switch via TogglePattern."""
    return _call(session_id, "toggle_control", control_id=control_id,
                 hwnd=_opt(hwnd), listing=_opt(listing))


@mcp.tool
def select_control(session_id: str, control_id: ControlId, hwnd: Hwnd = 0,
                   listing: Listing = "") -> Dict[str, Any]:
    """Select a list item, tab or navigation item via SelectionItemPattern."""
    return _call(session_id, "select_control", control_id=control_id,
                 hwnd=_opt(hwnd), listing=_opt(listing))


@mcp.tool
def expand_control(
    session_id: str,
    control_id: ControlId,
    collapse: Annotated[bool, Field(description="Collapse instead of expanding.")] = False,
    hwnd: Hwnd = 0,
    listing: Listing = "",
) -> Dict[str, Any]:
    """Expand or collapse a tree item, combo box or expander."""
    return _call(session_id, "expand_control", control_id=control_id,
                 collapse=collapse, hwnd=_opt(hwnd), listing=_opt(listing))


@mcp.tool
def scroll_control(
    session_id: str,
    control_id: ControlId,
    direction: Annotated[str, Field(description="up, down, left or right.")] = "down",
    amount: Annotated[int, Field(description="Number of lines, 1-10. Clamped: each line is a separate COM call, so a large amount is slow rather than far. Use to_percent to jump.")] = 3,
    hwnd: Hwnd = 0,
    to_percent: Annotated[float, Field(
        description="Jump straight to this scroll position, 0-100. One call "
                    "whatever the distance; 100 is the bottom. Negative, the "
                    "default, scrolls by `amount` instead.",
        examples=[100.0],
    )] = -1.0,
    listing: Listing = "",
) -> Dict[str, Any]:
    """Scroll a control via ScrollPattern.

    The named control rarely owns the scrolling itself - a page root or a
    NavigationView holds a ScrollViewer somewhere inside - so the nearest
    scrollable descendant, then ancestor, is used. The response's `scrolled`
    field says whether that was the control itself, a descendant or an ancestor.

    There is no wheel fallback: mouse wheel goes through SendInput, which a
    background desktop cannot receive.

    The response says where the scroller now sits - `vertical_percent`, `at_top`,
    `at_bottom`, `vertical_view_size` (the share of the content visible) - so
    "did that go far enough" costs nothing instead of a screenshot.

    To bring one specific control into view, use scroll_to_control rather than
    guessing an amount.
    """
    return _call(
        session_id, "scroll_control", control_id=control_id,
        direction=direction, amount=amount, hwnd=_opt(hwnd),
        to_percent=to_percent if to_percent >= 0 else None,
        listing=_opt(listing),
    )


@mcp.tool
def scroll_to_control(
    session_id: str,
    control_id: Annotated[str, Field(description="Id of the control to bring into view.", examples=["c14"])],
    hwnd: Hwnd = 0,
    listing: Listing = "",
) -> Dict[str, Any]:
    """Scroll until a control is in view, whatever kind of control it is.

    Tries ScrollItemPattern.ScrollIntoView, then SelectionItemPattern, then works
    the position out from the scroller's percent and view size. The response
    names which fired, gives the rect before and after, and says whether the
    control now lies inside the scroller's viewport.

    Use this rather than scroll_control with a guessed amount. select_control
    also scrolls, but only for things that are selectable - an Expander, a
    SettingsCard or a Group is not, and that gap is what turns one call into a
    guess-and-screenshot loop.

    Note the second route selects the control, which on a NavigationViewItem or a
    ListViewItem also *activates* it. Check the `method` in the response before
    reading anything into what the app did next.
    """
    return _call(
        session_id, "scroll_to_control", control_id=control_id, hwnd=_opt(hwnd),
        listing=_opt(listing),
    )


# ---------------------------------------------------------------------------
# actions - coordinates, for UI with no usable control tree
# ---------------------------------------------------------------------------


@mcp.tool
def click_xy(
    session_id: str,
    x: Annotated[int, Field(description="Absolute screen x on the isolated desktop, as reported in control and window rects.")],
    y: Annotated[int, Field(description="Absolute screen y on the isolated desktop, as reported in control and window rects.")],
    button: Annotated[str, Field(description="left or right. Only the PostMessage path has a notion of a button; a UIA pattern invoke does not.")] = "left",
    double: Annotated[bool, Field(description="Double-click. Only the PostMessage path can express this; a UIA pattern invoke ignores it.")] = False,
    method: Annotated[Literal["auto", "uia", "postmessage"], Field(
        description="'auto' resolves through UIA and falls back to PostMessage; "
                    "'uia' refuses rather than falling back; 'postmessage' posts "
                    "directly.",
    )] = "auto",
) -> Dict[str, Any]:
    """Click at absolute coordinates inside the isolated session.

    Resolves the UIA element under the point and invokes its pattern - it lands
    on controls, not pixels, and may resolve to an ancestor or a sibling of what
    is visually at that point. When the resolved element's rect does not contain
    the point, the response says so under `retargeted`; read that before
    concluding anything about what the app did next.

    SendInput is not used and cannot be: a background desktop does not own the
    input queue, so synthetic hardware input is discarded. With `method="auto"`
    an unresolvable point falls back to PostMessage, which WinUI often ignores
    while still reporting success - check `method` in the response, or pass
    `method="uia"` to be refused instead of silently falling back.

    A point on the window frame's own Minimize, Maximize, Close or system menu
    is refused, the same way click_control refuses one by id. That question is
    put to the window itself (WM_NCHITTEST) rather than to UIA, because UIA
    cannot answer it on WinUI 3: hit-testing anywhere in the caption strip
    returns one Pane spanning the whole strip, not the button that is there.

    Prefer click_control when the target has a control id.
    """
    return _call(session_id, "click_xy", x=x, y=y, button=button, double=double,
                 method=method)


@mcp.tool
def window_command(
    session_id: str,
    command: Annotated[str, Field(description="'restore' to un-minimize, or 'move' to reposition/resize.")],
    hwnd: Hwnd = 0,
    x: Annotated[int, Field(description="New left edge, for 'move'.")] = 0,
    y: Annotated[int, Field(description="New top edge, for 'move'.")] = 0,
    width: Annotated[int, Field(description="New width, for 'move'.")] = 0,
    height: Annotated[int, Field(description="New height, for 'move'.")] = 0,
    units: Annotated[str, Field(description="'dips' (default) or 'px'. XAML adaptive thresholds are in DIPs, so dips is almost always what you want.")] = "dips",
) -> Dict[str, Any]:
    """Restore or move/resize a window in the session.

    Sizes default to DIPs, matching AdaptiveTrigger.MinWindowWidth, so resizing
    to a breakpoint needs no scale arithmetic. The response reports the resulting
    rect in both units alongside `dpi_scale`.
    """
    return _call(
        session_id, "window_command", command=command, hwnd=_opt(hwnd),
        x=x, y=y, width=width, height=height, units=units,
    )


# ---------------------------------------------------------------------------
# debugging
# ---------------------------------------------------------------------------


@mcp.tool
def session_show(session_id: str) -> Dict[str, Any]:
    """Switch the physical display to this session's desktop so a human can watch.

    This DELIBERATELY BREAKS ISOLATION: the isolated desktop takes over the
    screen and the user loses sight of their own desktop until session_hide is
    called. Only use it when the user explicitly asks to see the session.
    """
    try:
        return manager.get(session_id).show()
    except SessionError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
def session_hide() -> Dict[str, Any]:
    """Switch the physical display back to the user's own desktop."""
    return manager.hide()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
