"""How a listing id finds its live element, and two refusals. No desktop needed.

Ids used to resolve by (name, control_type, automation_id), first match wins.
Every control sharing that triple went to the first one - the second of two
unnamed TextBoxes, the second "Source code" Expander, XML Hub's output editor -
while the echo still showed the row that was asked for. And a RichEdit, whose
name is its content, went STALE as soon as `set_text` changed it.
"""

from __future__ import annotations

import pytest

from isodesk import uia


class _Info:
    def __init__(self, control_type, automation_id):
        self.control_type = control_type
        self.automation_id = automation_id


class _Element:
    def __init__(self, rid, name, control_type="Edit", automation_id=""):
        self._isodesk_rid = rid
        self._name = name
        self.element_info = _Info(control_type, automation_id)

    def window_text(self):
        return self._name


def _row(element, control_id="c0"):
    return {
        "id": control_id,
        "name": element._name,
        "control_type": element.element_info.control_type,
        "automation_id": element.element_info.automation_id,
        "runtime_id": element._isodesk_rid,
    }


def test_twins_resolve_to_themselves():
    first, second = _Element((42, 1), ""), _Element((42, 2), "")
    assert uia._find_live([first, second], _row(second, "c1")) is second


def test_a_renamed_control_still_resolves():
    box = _Element((42, 7), "")
    row = _row(box)
    box._name = "abc"  # set_text on a control whose name is its content
    assert uia._find_live([box], row) is box


def test_the_runtime_id_survives_a_round_trip_as_a_list():
    box = _Element((42, 7), "x")
    row = _row(box)
    row["runtime_id"] = list(row["runtime_id"])
    assert uia._find_live([box], row) is box


def test_a_gone_element_falls_back_to_a_unique_identity():
    old = _Element((42, 1), "Save", "Button", "SaveButton")
    recreated = _Element((42, 9), "Save", "Button", "SaveButton")
    assert uia._find_live([recreated], _row(old)) is recreated


def test_a_gone_element_with_twins_is_ambiguous_not_guessed():
    old = _Element((42, 1), "")
    twins = [_Element((42, 8), ""), _Element((42, 9), "")]
    with pytest.raises(uia.Ambiguous):
        uia._find_live(twins, _row(old))


def test_a_reused_runtime_id_on_another_type_is_not_a_match():
    old = _Element((42, 1), "Go", "Button")
    reused = _Element((42, 1), "Go", "Edit")
    assert uia._find_live([reused], _row(old)) is None


@pytest.mark.parametrize("described, expected", [
    ({"name": "Close", "automation_id": "Close", "class_name": ""}, True),
    ({"name": "Close", "automation_id": "", "class_name": None}, True),
    # A classic Win32 ComboBox's drop-down arrow, from Character Map.
    ({"name": "Close", "automation_id": "DropDown", "class_name": ""}, False),
    # A ComboBox light-dismiss overlay.
    ({"name": "Close", "automation_id": "Light Dismiss", "class_name": "ComboBoxLightDismiss"}, False),
])
def test_caption_buttons(described, expected):
    assert uia.is_caption_button(described) is expected


def test_a_tool_parameter_named_method_reaches_the_session():
    # click_xy takes `method`; before `_call` made its own positional-only, the
    # two collided and every click_xy failed with "multiple values for argument".
    from fastmcp.exceptions import ToolError

    from isodesk.mcp_server import _call

    with pytest.raises(ToolError, match="no session"):
        _call("isodesk-none", "click_xy", x=1, y=1, method="uia")
