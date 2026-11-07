"""Wire formatting for control listings.

The worker keeps everything it knows about a control in the snapshot dict:
`uia.resolve_control` matches actions against that identity, `capture.annotate`
draws boxes from the physical rect, and `uia._contained_in_actionable` dedupes
with it. None of that means the whole dict is worth an agent's context. This
module is the single place that decides what actually crosses the wire, so the
listing can get smaller without the snapshot losing anything.

Three reductions, in the order they matter. A 40-row WinUI 3 settings page cost
about 5k characters of JSON in the session that prompted this:

  * `rect` and `rect_dips` are the same numbers times `dpi_scale`, which is
    already reported once per listing. Only one of them is layout truth for XAML
    - adaptive thresholds are written in DIPs - so `units="dips"` emits that one.
  * `automation_id: ""`, `class_name: ""`, `xaml_type: null`, `enabled: true`,
    `visible: true` sat on nearly every row. Only deviations from the default are
    emitted; absence means the default.
  * JSON's punctuation and repeated keys are roughly half of what is left, and a
    control listing is a table, not a document. `format="compact"` emits one line
    per control.

`tests/smoke_lean_scope.py` asserts the compact rendering of a real listing stays
under 40% of the JSON one, which is the bar these three together have to clear.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

UNITS = ("dips", "px", "both")
FORMATS = ("compact", "json")

# A ValuePattern can hold an entire document (a RichEditBox), and no listing
# should be able to grow without bound because one text box is full.
_MAX_VALUE_CHARS = 60


class RenderError(ValueError):
    pass


def check(units: str, fmt: str) -> None:
    if units not in UNITS:
        raise RenderError(f"units must be one of {UNITS}, got {units!r}")
    if fmt not in FORMATS:
        raise RenderError(f"format must be one of {FORMATS}, got {fmt!r}")


# ---------------------------------------------------------------------------
# field selection - shared by both formats, so they never disagree
# ---------------------------------------------------------------------------


def _redundant_class_name(class_name: Optional[str], xaml_type: Optional[str]) -> bool:
    """Is `class_name` just `xaml_type` with its namespace still attached?

    `xaml_type_of` strips known prefixes, so for every XAML control the raw
    class_name either equals the xaml_type or is the same name behind a
    namespace. Neither tells an agent anything the xaml_type did not.
    """
    if not class_name or not xaml_type:
        return False
    return class_name == xaml_type or class_name.endswith("." + xaml_type)


def _rect_for(control: Dict[str, Any], units: str) -> Optional[Dict[str, int]]:
    """The rect to print. DIPs fall back to pixels when no scale was available."""
    if units == "px":
        return control.get("rect")
    return control.get("rect_dips") or control.get("rect")


def state_words(control: Dict[str, Any]) -> List[str]:
    """The control's state as bare words, for the compact row.

    Only deviations are emitted, which is what makes a row short: a control that
    is enabled, visible and unselected says nothing about any of them. The one
    subtlety is `None`, which `uia._safe` produces when the property would not
    read - that is *unknown*, not `true`, and it is reported as such rather than
    silently defaulting.
    """
    words: List[str] = []

    toggle = control.get("toggle_state")
    if toggle:
        words.append(str(toggle))
    if control.get("selected"):
        words.append("selected")
    expanded = control.get("expanded")
    # "leaf" is ExpandCollapse's way of saying "nothing to expand", which is the
    # default state of most of a tree; printing it on every row buys nothing.
    if expanded and expanded != "leaf":
        words.append(str(expanded))

    value = control.get("value")
    if value:
        text = str(value)
        if len(text) > _MAX_VALUE_CHARS:
            text = text[:_MAX_VALUE_CHARS] + "..."
        words.append('="' + text.replace('"', '\\"') + '"')

    enabled = control.get("enabled")
    if enabled is False:
        words.append("disabled")
    elif enabled is None:
        words.append("enabled?")

    visible = control.get("visible")
    if visible is False:
        words.append("hidden")
    elif visible is None:
        words.append("visible?")

    # UIA reports the *clipped* BoundingRectangle, so a control cut down by a
    # scroll viewport looks like a small control rather than a hidden one. The
    # rect alone cannot say which; this word can.
    if control.get("clipped"):
        words.append("clipped")

    if control.get("caption_button"):
        words.append("caption")
    return words


def trim(control: Dict[str, Any], units: str) -> Dict[str, Any]:
    """The JSON form of one control: same field selection as the compact row."""
    out: Dict[str, Any] = {"id": control.get("id")}

    for key in ("name", "control_type", "xaml_type", "automation_id"):
        value = control.get(key)
        if value:
            out[key] = value

    class_name = control.get("class_name")
    if class_name and not _redundant_class_name(class_name, control.get("xaml_type")):
        out["class_name"] = class_name

    if units == "both":
        for key in ("rect", "rect_dips"):
            if control.get(key):
                out[key] = control[key]
    else:
        rect = _rect_for(control, units)
        if rect:
            out["rect" if units == "px" else "rect_dips"] = rect

    for key in ("toggle_state", "selected", "expanded", "value"):
        value = control.get(key)
        # False and "leaf" are the defaults; see state_words.
        if value and value != "leaf":
            out[key] = value

    for key in ("enabled", "visible"):
        if control.get(key) is not True:
            out[key] = control.get(key)

    if control.get("clipped"):
        out["clipped"] = True
    if control.get("caption_button"):
        out["caption_button"] = True
    return out


# ---------------------------------------------------------------------------
# compact rows
# ---------------------------------------------------------------------------


def _type_word(control: Dict[str, Any]) -> str:
    """`Expander[Button]` - the XAML type, with UIA's lossy view only when it differs.

    UIA reports a ToggleSwitch, an Expander and a plain Button all as `Button`, so
    the xaml_type leads. The control_type is still worth a few characters because
    it is what decides which action verbs apply.
    """
    xaml_type = control.get("xaml_type")
    control_type = control.get("control_type")
    if xaml_type and control_type and xaml_type != control_type:
        return f"{xaml_type}[{control_type}]"
    return xaml_type or control_type or "?"


def _rect_word(control: Dict[str, Any], units: str) -> str:
    if units == "both":
        px, dips = control.get("rect"), control.get("rect_dips")
        parts = []
        if dips:
            parts.append(f"{dips['left']},{dips['top']} {dips['width']}x{dips['height']}")
        if px:
            parts.append(f"px {px['left']},{px['top']} {px['width']}x{px['height']}")
        return " ".join(parts)
    rect = _rect_for(control, units)
    if not rect:
        return ""
    return f"{rect['left']},{rect['top']} {rect['width']}x{rect['height']}"


def row(control: Dict[str, Any], units: str = "dips") -> str:
    """One control, one line.

        c23 Expander[Button] "Startup" #StartupCard 472,558 716x70 expanded

    Grammar: id, type, quoted name, #automation_id, left,top WxH, then state
    words. Name and automation_id are dropped when empty rather than quoted as
    empty, which is most of what makes this shorter than the JSON row.
    """
    parts = [str(control.get("id")), _type_word(control)]

    name = control.get("name")
    if name:
        parts.append('"' + str(name).replace('"', '\\"') + '"')

    automation_id = control.get("automation_id")
    if automation_id:
        parts.append("#" + str(automation_id))

    rect_word = _rect_word(control, units)
    if rect_word:
        parts.append(rect_word)

    parts.extend(state_words(control))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# whole payloads
# ---------------------------------------------------------------------------


def _header(payload: Dict[str, Any], units: str) -> str:
    """One line of context, so no row has to repeat any of it."""
    bits = [f"controls {payload.get('count')} of {payload.get('total_matched')}"]
    if units == "dips":
        bits.append("dips")
    elif units == "px":
        bits.append("px")
    scale = payload.get("dpi_scale")
    if scale:
        bits.append(f"dpi {scale}")
    if payload.get("listing"):
        bits.append(f"listing {payload['listing']}")
    if payload.get("hwnd"):
        bits.append(f"hwnd {payload['hwnd']}")
    if payload.get("scope"):
        bits.append(f"scope {payload['scope']}")
    for key in ("xaml_type", "control_type", "name_contains"):
        if payload.get("filters", {}).get(key):
            bits.append(f"{key}={payload['filters'][key]}")
    return " | ".join(bits)


def _truncation_note(payload: Dict[str, Any]) -> Optional[str]:
    if not payload.get("truncated"):
        return None
    return (
        f"TRUNCATED: {payload.get('count')} of {payload.get('total_matched')} shown. "
        "The omitted controls are latest in tree order - on a scrolled page, exactly "
        "the content you are looking for. Narrow with scope= or a filter."
    )


def listing(payload: Dict[str, Any], units: str = "dips", fmt: str = "compact") -> str:
    """Render a `uia.list_controls` payload for the wire."""
    check(units, fmt)
    controls = payload.get("controls", [])

    if fmt == "json":
        out = {
            key: payload[key]
            for key in (
                "hwnd", "listing", "count", "total_matched", "total_descendants",
                "truncated", "dpi_scale", "scope", "filters", "units",
            )
            if key in payload
        }
        out["units"] = units
        out["controls"] = [trim(control, units) for control in controls]
        return json.dumps(out, ensure_ascii=False)

    lines = [_header(payload, units)]
    lines.extend(row(control, units) for control in controls)
    if not controls:
        lines.append(
            "(no controls matched - widen the filter, or try "
            "include_unnamed=true, actionable_only=false)"
        )
    note = _truncation_note(payload)
    if note:
        lines.append(note)
    return "\n".join(lines)


def refreshed(payload: Dict[str, Any], units: str = "dips", fmt: str = "compact") -> str:
    """Render a targeted re-read: one line per id, live row or `STALE`."""
    check(units, fmt)
    rows = payload.get("rows", [])

    if fmt == "json":
        out = dict(payload)
        out["units"] = units
        out["rows"] = [
            entry if entry.get("stale") else trim(entry, units) for entry in rows
        ]
        return json.dumps(out, ensure_ascii=False)

    lines = []
    scale = payload.get("dpi_scale")
    head = ["refresh"]
    if units != "both":
        head.append(units)
    if scale:
        head.append(f"dpi {scale}")
    if payload.get("listing"):
        head.append(f"listing {payload['listing']}")
    lines.append(" | ".join(head))
    for entry in rows:
        if entry.get("stale"):
            note = (
                ", and several live controls match its description"
                if entry.get("ambiguous") else ""
            )
            lines.append(
                f"{entry['id']} STALE - {entry.get('was', '')} is no longer in the tree{note}"
            )
        else:
            lines.append(row(entry, units))
    return "\n".join(lines)


def measurement(payload: Dict[str, Any], units: str = "dips", fmt: str = "compact") -> str:
    """Render `uia.measure`: the rects, then the pairwise deltas."""
    check(units, fmt)
    if fmt == "json":
        return json.dumps({**payload, "units": units}, ensure_ascii=False)

    lines = [f"measure | {units} | dpi {payload.get('dpi_scale')}"]
    for entry in payload.get("controls", []):
        name = entry.get("name")
        label = f'{entry["id"]} "{name}"' if name else str(entry["id"])
        rect = entry.get("rect") or {}
        lines.append(
            f"{label} {rect.get('left')},{rect.get('top')} "
            f"{rect.get('width')}x{rect.get('height')}"
        )
    for pair in payload.get("pairs", []):
        notes = " | ".join(pair.get("notes", []))
        lines.append(
            f"{pair['a']}->{pair['b']} dx {pair['dx']} dy {pair['dy']} "
            f"dw {pair['dw']} dh {pair['dh']}" + (f" | {notes}" if notes else "")
        )
    return "\n".join(lines)
