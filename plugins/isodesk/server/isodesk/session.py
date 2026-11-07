"""Host-side session lifecycle.

A session is: one desktop object + one worker process on it + one named pipe.
The host holds the desktop handle open for the session's lifetime, because a
desktop is destroyed once its last handle closes and no threads remain attached.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from .ipc import IpcError, PipeClient
from .win32 import desktop as dt
from .win32 import process as pr


class SessionError(RuntimeError):
    pass


# Every call gets a wall-clock deadline. Nothing on this surface should take
# more than a few seconds - a control enumeration of a large window is under
# half a second - so a call still running at 90 is wedged, not slow.
DEFAULT_CALL_TIMEOUT = 90.0
# Three handlers carry their own waiting period; the host must outlast it rather
# than declaring a timeout while the worker is still doing what it was asked.
_TIMEOUT_HEADROOM = 30.0


def _call_timeout(method: str, params: Dict[str, Any]) -> float:
    if method == "run":
        return float(params.get("timeout") or 60.0) + _TIMEOUT_HEADROOM
    if method in ("launch", "activate"):
        return float(params.get("wait") or 8.0) + _TIMEOUT_HEADROOM
    return DEFAULT_CALL_TIMEOUT


class Session:
    """One isolated Windows desktop, with a worker process serving it."""

    def __init__(self, session_id: str, label: str = ""):
        self.id = session_id
        self.label = label
        self.created_at = time.time()
        self.hdesk: Optional[int] = None
        self.worker_pid: Optional[int] = None
        self.client: Optional[PipeClient] = None
        self.log_path = os.path.join(
            tempfile.gettempdir(), f"isodesk-{session_id}.log"
        )
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self, connect_timeout: float = 45.0) -> "Session":
        self.hdesk = dt.create_desktop(self.id)

        worker_cmd = (
            f'"{sys.executable}" -m isodesk.worker '
            f'--session-id "{self.id}" --log "{self.log_path}"'
        )
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")

        try:
            self.worker_pid = pr.launch_on_desktop(
                worker_cmd,
                dt.desktop_path(self.id),
                cwd=repo_root,
                env=env,
                no_window=True,
            )
        except Exception as exc:
            self.destroy()
            raise SessionError(f"could not start worker for session {self.id}: {exc}") from exc

        try:
            self.client = PipeClient(self.id).connect(timeout=connect_timeout)
        except IpcError as exc:
            detail = self._worker_log_tail()
            self.destroy()
            raise SessionError(
                f"worker for session {self.id} never came up: {exc}\n"
                f"worker log:\n{detail}"
            ) from exc

        return self

    def destroy(self) -> Dict[str, Any]:
        """Tear the session down: stop the worker, kill its apps, close the desktop."""
        killed: List[int] = []

        if self.client:
            try:
                killed = list(self.client.call("shutdown").get("reaped") or [])
            except Exception:
                pass  # the worker may already be gone; the kill sweep covers it
            self.client.close()
            self.client = None

        if self.hdesk:
            try:
                killed += pr.kill_desktop_processes(
                    self.hdesk,
                    dt.current_thread_desktop(),
                    extra_pids={self.worker_pid} if self.worker_pid else None,
                )
            except Exception:
                pass
            dt.close_desktop(self.hdesk)
            self.hdesk = None

        return {"session_id": self.id, "killed_pids": killed}

    # -- rpc ---------------------------------------------------------------

    def call(self, method: str, /, **params) -> Dict[str, Any]:
        """Send one RPC to the worker.

        `method` and `self` are positional-only so that a tool parameter may be
        called `method` without colliding with the name of the RPC being sent -
        which is exactly what `click_xy(method="uia")` did.
        """
        if not self.client:
            raise SessionError(f"session {self.id} is not running")

        timeout = _call_timeout(method, params)
        # One pipe instance, so requests must not interleave. The timeout matters
        # as much as the lock: a caller queued behind a wedged call used to block
        # here forever, which looked like the session had died rather than like
        # one call being slow.
        if not self._lock.acquire(timeout=timeout):
            raise SessionError(
                f"another call on session {self.id} is still running after "
                f"{timeout}s; retry, or destroy the session"
            )
        try:
            return self.client.call(method, timeout, **params)
        finally:
            self._lock.release()

    # -- introspection -----------------------------------------------------

    def _worker_log_tail(self, lines: int = 20) -> str:
        try:
            with open(self.log_path, encoding="utf-8") as fh:
                return "".join(fh.readlines()[-lines:])
        except Exception:
            return "(no worker log)"

    def info(self) -> Dict[str, Any]:
        return {
            "session_id": self.id,
            "label": self.label,
            "desktop": dt.desktop_path(self.id),
            "worker_pid": self.worker_pid,
            "running": self.client is not None,
            "uptime_seconds": round(time.time() - self.created_at, 1),
            "log": self.log_path,
        }

    def show(self) -> Dict[str, Any]:
        """Switch the physical display to this session's desktop.

        This deliberately breaks isolation and is only for a human wanting to
        look. Switching back is `SwitchDesktop` on the default desktop, which
        `SessionManager.hide()` does.
        """
        if not self.hdesk:
            raise SessionError(f"session {self.id} is not running")
        dt.switch_desktop(self.hdesk)
        return {"switched_to": self.id}


class SessionManager:
    """Registry of live sessions for one host process."""

    def __init__(self, prefix: str = "isodesk"):
        self.prefix = prefix
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()
        self._default_desktop: Optional[int] = None

    def create(self, label: str = "") -> Session:
        session_id = f"{self.prefix}-{uuid.uuid4().hex[:10]}"
        session = Session(session_id, label=label).start()
        with self._lock:
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionError(
                f"no session {session_id!r}; live sessions: {list(self._sessions)}"
            )
        return session

    def list(self) -> List[Dict[str, Any]]:
        return [s.info() for s in self._sessions.values()]

    def destroy(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            raise SessionError(f"no session {session_id!r}")
        return session.destroy()

    def orphans(self) -> List[Dict[str, Any]]:
        """Desktops this server's naming scheme created but no live session owns.

        A host that exits cleanly runs `destroy_all` through its atexit hook. One
        that is killed does not, and what it leaves behind is not merely untidy:
        the app still running on that desktop holds its own binaries open, so the
        next build or deploy-layout copy blocks on the lock - and it does so
        silently, retrying for minutes, with nothing pointing at the invisible
        process that is the cause.
        """
        live = set(self._sessions)
        found: List[Dict[str, Any]] = []
        for name in dt.enum_desktops():
            if not name.startswith(self.prefix + "-") or name in live:
                continue
            entry: Dict[str, Any] = {"session_id": name, "orphan": True}
            try:
                hdesk = dt.open_desktop(name)
            except Exception as exc:
                entry["error"] = f"cannot open: {exc}"
                found.append(entry)
                continue
            try:
                windows = dt.app_windows(hdesk)
                entry["windows"] = [
                    {"title": w["title"], "class": w["class"], "pid": w["pid"]}
                    for w in windows
                ]
                entry["pids"] = sorted({w["pid"] for w in windows})
            finally:
                dt.close_desktop(hdesk)
            found.append(entry)
        return found

    def reap(self, session_id: str) -> List[Dict[str, Any]]:
        """Kill and close orphaned desktops. `session_id="all"` takes every one."""
        targets = self.orphans()
        if session_id != "all":
            targets = [t for t in targets if t["session_id"] == session_id]
            if not targets:
                raise SessionError(
                    f"no orphaned desktop {session_id!r}; live sessions are "
                    f"{list(self._sessions)}"
                )

        reaped: List[Dict[str, Any]] = []
        host_hdesk = dt.current_thread_desktop()
        for target in targets:
            name = target["session_id"]
            try:
                hdesk = dt.open_desktop(name)
            except Exception as exc:
                reaped.append({"session_id": name, "error": str(exc)})
                continue
            try:
                # Same guard as a normal teardown: a pid that also owns a window
                # on the host desktop is a host process and is never touched.
                killed = pr.kill_desktop_processes(hdesk, host_hdesk)
            finally:
                dt.close_desktop(hdesk)
            reaped.append({"session_id": name, "killed_pids": killed})
        return reaped

    def destroy_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        return [s.destroy() for s in sessions]

    def hide(self) -> Dict[str, Any]:
        """Switch the display back to the user's default desktop."""
        hdesk = dt.open_desktop("Default")
        dt.switch_desktop(hdesk)
        return {"switched_to": "Default"}
