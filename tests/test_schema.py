"""The emitted tool schema, checked from outside Python.

This is the only test here that needs no desktop, no session and no app: it
imports the server and reads the JSON schema FastMCP generates for every
registered tool.

It exists because the failure it guards is invisible from inside Python. An
`Optional[List[int]] = None` parameter is perfectly well typed to `inspect` and
to pydantic, and every call from a Python test passes - but pydantic renders it
as an `anyOf` with no top-level `"type"`, and a client that keys its parameter
rendering and its argument coercion off `schema.type` then shows the caller an
empty schema and passes the argument through uncoerced. A *correct* argument
then fails with a type error exactly as a guessed one would.

That is what happened: a model driving isodesk saw `crop` and about half the
other parameters arrive as `{}` - no type, no description - and the failure
partitioned exactly along `Optional[...]`. Every plain-typed parameter worked.

So the rule the surface has to keep is: no nullable unions in the public schema.
Concrete type, sentinel default, and `mcp_server._opt` normalizes the sentinel
back to None at the worker boundary.
"""

from __future__ import annotations

import asyncio

import pytest

from isodesk.mcp_server import mcp


@pytest.fixture(scope="module")
def tools():
    return asyncio.run(mcp.list_tools())


def _properties(tool):
    return (tool.parameters or {}).get("properties") or {}


def test_every_property_has_a_top_level_type(tools):
    """No `anyOf`, ever. This is the whole point of the file."""
    offenders = [
        f"{tool.name}.{name}: {prop}"
        for tool in tools
        for name, prop in _properties(tool).items()
        if "type" not in prop or "anyOf" in prop
    ]
    assert not offenders, "nullable/untyped parameters:\n" + "\n".join(offenders)


def test_every_property_has_a_description(tools):
    """`session_id` is self-evident everywhere; nothing else is.

    A parameter with a type but no description is half the reported failure -
    the caller can tell it is an array of integers and still has to guess
    whether that is [x, y, w, h] or [left, top, right, bottom], and in which
    frame and which units.
    """
    offenders = [
        f"{tool.name}.{name}"
        for tool in tools
        for name, prop in _properties(tool).items()
        if name != "session_id" and not prop.get("description")
    ]
    assert not offenders, "parameters with no description: " + ", ".join(offenders)


def test_crop_states_its_shape_frame_and_units(tools):
    """The parameter that was guessed blind carries the answer itself.

    Frame and units used to live one line below, in `units`, which is where the
    reader was not looking.
    """
    crop = _properties(next(t for t in tools if t.name == "screenshot"))["crop"]
    assert crop["type"] == "array"
    assert crop.get("minItems") == 4 and crop.get("maxItems") == 4
    assert crop.get("examples"), "crop needs a worked example of its shape"
    description = crop["description"].lower()
    assert "screen-absolute" in description
    assert "dips" in description


def test_sentinels_normalize_back_to_none():
    """`_opt` is what lets a concrete type mean "unset" at the worker boundary."""
    from isodesk.mcp_server import _opt

    assert _opt(0) is None
    assert _opt("") is None
    assert _opt([]) is None
    assert _opt(None) is None
    assert _opt(12345) == 12345
    assert _opt("L4") == "L4"
    assert _opt([1, 2, 3, 4]) == [1, 2, 3, 4]
