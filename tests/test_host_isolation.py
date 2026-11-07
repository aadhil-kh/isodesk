"""The invariant that justifies this whole project.

Everything isodesk does must be invisible to the user's own desktop. The Phase 0
spike compared cursor position and foreground window before and after, which
turned out to be useless: it reports a violation whenever the user simply moves
their mouse during the run.

So these tests are sentinel-based instead. A Notepad is opened on the HOST
desktop with known content, then a session types and clicks aggressively in
isolation. If any of that input escaped, the host sentinel would change - it must
not. That is a claim about our behaviour, not about whether the user sat still.

    python -m pytest tests/test_host_isolation.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins", "isodesk", "server"))

from isodesk import SessionManager  # noqa: E402
from isodesk.win32 import desktop as dt  # noqa: E402
from isodesk.win32 import process as pr  # noqa: E402

SENTINEL_TEXT = "ISODESK_SENTINEL_DO_NOT_MODIFY"
TYPED_PAYLOAD = "if you can read this on the host desktop the isolation is broken"


@pytest.fixture(scope="module")
def manager():
    mgr = SessionManager()
    yield mgr
    mgr.destroy_all()


@pytest.fixture
def session(manager):
    sess = manager.create(label="isolation-test")
    yield sess
    manager.destroy(sess.id)


def _read_host_sentinel_text(path: str) -> str:
    """Read the live text of the host sentinel Notepad via UIA.

    Reading the file on disk would be a useless check - Notepad does not write
    until saved, so stray keystrokes would never show up there. The window's
    actual document text is the real evidence.
    """
    from pywinauto import Desktop

    name = os.path.basename(path)
    for window in Desktop(backend="uia").windows():
        try:
            if name.split(".")[0] not in window.window_text():
                continue
            for element in window.descendants():
                info = getattr(element, "element_info", None)
                if getattr(info, "control_type", None) in ("Document", "Edit"):
                    try:
                        value = element.get_value()
                    except Exception:
                        value = element.window_text()
                    if value:
                        return value
        except Exception:
            continue
    raise AssertionError(f"host sentinel window for {name!r} not found")


@pytest.fixture
def host_sentinel():
    """A Notepad on the USER's desktop holding known text."""
    import tempfile

    path = os.path.join(tempfile.gettempdir(), "isodesk_sentinel.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(SENTINEL_TEXT)

    proc = subprocess.Popen(["notepad.exe", path])
    time.sleep(4)
    yield path

    for pid in {
        w["pid"]
        for w in dt.app_windows(dt.current_thread_desktop())
        if "isodesk_sentinel" in w["title"]
    } | {proc.pid}:
        try:
            pr.kill_process_tree(pid)
        except Exception:
            pass
    try:
        os.remove(path)
    except Exception:
        pass


def _host_windows():
    return dt.app_windows(dt.current_thread_desktop())


def test_session_windows_never_appear_on_host(session):
    """Apps launched into a session must not be visible on the user's desktop."""
    before = {w["hwnd"] for w in _host_windows()}

    session.call("launch", cmdline="notepad.exe", wait=8)
    result = session.call("list_windows")
    assert result["windows"], "the app did not start inside the session"

    session_hwnds = {w["hwnd"] for w in result["windows"]}
    after = {w["hwnd"] for w in _host_windows()}

    leaked = session_hwnds & after
    assert not leaked, f"session windows leaked onto the host desktop: {leaked}"
    assert after == before, f"host desktop gained/lost windows: {after ^ before}"


def test_input_does_not_reach_the_host(session, host_sentinel):
    """Input driven inside a session must not land in a host window."""
    session.call("launch", cmdline="notepad.exe", wait=8)
    windows = session.call("list_windows")["windows"]
    target = next(w for w in windows if w["class"] == "Notepad")

    # Click and set text repeatedly inside the session, to give any leak a fair
    # chance to show up on the host.
    for _ in range(3):
        try:
            session.call("click_xy",
                         x=(target["rect"]["left"] + target["rect"]["right"]) // 2,
                         y=(target["rect"]["top"] + target["rect"]["bottom"]) // 2)
        except Exception:
            pass  # nothing invokable under that point is fine; the click still ran
        controls = session.call("get_controls", hwnd=target["hwnd"])["controls"]
        editable = next(
            (c for c in controls if c["control_type"] in ("Edit", "Document")), None
        )
        if editable:
            try:
                session.call("set_text", control_id=editable["id"],
                             hwnd=target["hwnd"], text=TYPED_PAYLOAD)
            except Exception:
                pass  # Notepad's document may not expose ValuePattern

    time.sleep(1)

    content = _read_host_sentinel_text(host_sentinel)
    assert TYPED_PAYLOAD not in content, (
        "text typed inside the session appeared in a host window - "
        "input escaped the isolation boundary"
    )
    assert content.strip() == SENTINEL_TEXT, (
        f"host sentinel content changed to {content!r} - input escaped the session"
    )


def test_session_never_takes_host_foreground(session):
    """No session window may ever become the host's foreground window.

    Deliberately NOT asserting the foreground window is unchanged: the user is
    free to switch between their own windows while a session runs, and an
    equality check would flag that as a failure. The real invariant is that
    nothing belonging to the session ever gets focus on the host.
    """
    session.call("launch", cmdline="notepad.exe", wait=8)
    windows = session.call("list_windows")["windows"]
    target = next(w for w in windows if w["class"] == "Notepad")

    session_hwnds = {w["hwnd"] for w in windows}
    session_pids = {w["pid"] for w in windows}

    # Capture and enumerate repeatedly - these are the operations that call
    # BringWindowToTop/SetForegroundWindow inside the session.
    for _ in range(3):
        session.call("get_controls", hwnd=target["hwnd"])
        session.call("screenshot", hwnd=target["hwnd"])

        fg = dt.foreground_window()
        assert fg not in session_hwnds, (
            f"a session window took host foreground: {dt.window_title(fg)!r}"
        )
        # A session process must not own the host's foreground window either.
        host_visible = {w["hwnd"]: w["pid"] for w in _host_windows()}
        assert host_visible.get(fg) not in session_pids or fg not in host_visible, (
            f"host foreground {dt.window_title(fg)!r} belongs to a session process"
        )


def test_sessions_are_isolated_from_each_other(manager):
    """Two sessions must not see or contend with each other's windows."""
    a = manager.create(label="a")
    b = manager.create(label="b")
    try:
        a.call("launch", cmdline="notepad.exe", wait=8)
        a_hwnds = {w["hwnd"] for w in a.call("list_windows")["windows"]}
        b_hwnds = {w["hwnd"] for w in b.call("list_windows")["windows"]}

        assert a_hwnds, "session a has no windows"
        assert not (a_hwnds & b_hwnds), "sessions can see each other's windows"
        assert not b_hwnds, f"session b should be empty, saw {b_hwnds}"
    finally:
        manager.destroy(a.id)
        manager.destroy(b.id)


def test_destroy_cleans_up_processes(manager):
    """Destroying a session must not leave its app processes running."""
    session = manager.create(label="cleanup")
    session.call("launch", cmdline="notepad.exe", wait=8)
    windows = session.call("list_windows")["windows"]
    pids = {w["pid"] for w in windows}
    assert pids

    manager.destroy(session.id)
    time.sleep(1.5)

    still_alive = []
    for pid in pids:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True
        )
        if str(pid) in result.stdout:
            still_alive.append(pid)
    assert not still_alive, f"processes survived session destroy: {still_alive}"


def test_keyboard_injection_fails_loudly(session):
    """send_keys must raise, not silently no-op.

    SendInput delivers zero events off the input desktop. An agent that believed
    it had pressed Ctrl+S would go on to make wrong decisions, so the failure
    has to be explicit and has to say what to use instead.
    """
    session.call("launch", cmdline="notepad.exe", wait=8)
    with pytest.raises(Exception) as excinfo:
        session.call("send_keys", keys="^s")
    message = str(excinfo.value)
    assert "set_text" in message and "click_control" in message, (
        f"error message must point at the working alternatives, got: {message}"
    )


def test_destroy_never_kills_host_processes(manager, host_sentinel):
    """The cleanup sweep must not touch processes that own host windows."""
    host_pids_before = {w["pid"] for w in _host_windows()}

    session = manager.create(label="safety")
    session.call("launch", cmdline="notepad.exe", wait=8)
    manager.destroy(session.id)
    time.sleep(1)

    host_pids_after = {w["pid"] for w in _host_windows()}
    killed = host_pids_before - host_pids_after
    assert not killed, f"session teardown killed host processes: {killed}"

    # The sentinel Notepad must still be alive and readable.
    assert _read_host_sentinel_text(host_sentinel).strip() == SENTINEL_TEXT
