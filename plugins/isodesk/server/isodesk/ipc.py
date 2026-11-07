"""Named-pipe JSON-RPC between the host (MCP server) and a session worker.

Framing is a 4-byte little-endian length prefix followed by UTF-8 JSON. A length
prefix is used rather than newline delimiting because responses carry
base64-encoded PNGs, which are large and must not be scanned byte-by-byte.

The pipe is created by the worker (which lives on the isolated desktop) and
connected to by the host. Default security applies, so only the same user can
open it.
"""

from __future__ import annotations

import json
import struct
import time
from typing import Any, Dict, Optional

import pywintypes
import win32file
import win32pipe

PIPE_PREFIX = r"\\.\pipe\isodesk-"
_LEN = struct.Struct("<I")
MAX_MESSAGE = 64 * 1024 * 1024  # screenshots are the large case


class IpcError(RuntimeError):
    pass


class IpcTimeout(IpcError):
    """A call did not answer inside its deadline; the worker may still be busy."""


def pipe_name(session_id: str) -> str:
    return f"{PIPE_PREFIX}{session_id}"


def _send(handle, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    win32file.WriteFile(handle, _LEN.pack(len(body)) + body)


def _wait_readable(handle, deadline: Optional[float], poll: float = 0.02) -> None:
    """Block until the pipe has bytes, or the deadline passes.

    `ReadFile` on a blocking byte-mode pipe has no timeout, so a worker stuck in
    a COM call held the host thread forever. Peeking first is what turns that
    into an error the caller can act on.
    """
    if deadline is None:
        return
    while True:
        try:
            _data, available, _left = win32pipe.PeekNamedPipe(handle, 0)
        except pywintypes.error as exc:
            raise IpcError(f"pipe closed while waiting for a response: {exc}") from exc
        if available:
            return
        if time.time() > deadline:
            raise IpcTimeout("no response within the call deadline")
        time.sleep(poll)


def _recv_exact(handle, count: int, deadline: Optional[float] = None) -> bytes:
    chunks = []
    remaining = count
    while remaining > 0:
        _wait_readable(handle, deadline)
        _hr, data = win32file.ReadFile(handle, remaining)
        if not data:
            raise IpcError("pipe closed mid-message")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def _recv(handle, deadline: Optional[float] = None) -> Dict[str, Any]:
    (length,) = _LEN.unpack(_recv_exact(handle, _LEN.size, deadline))
    if length > MAX_MESSAGE:
        raise IpcError(f"message of {length} bytes exceeds cap")
    # Once the length prefix has been read the rest of the message is already on
    # its way, so the body is read to completion rather than abandoned - a
    # half-consumed message would desynchronize the pipe permanently.
    return json.loads(_recv_exact(handle, length, None).decode("utf-8"))


class PipeServer:
    """Worker side: create the pipe, accept one client, serve requests."""

    def __init__(self, session_id: str):
        self.name = pipe_name(session_id)
        self.handle = None

    def create(self):
        self.handle = win32pipe.CreateNamedPipe(
            self.name,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            1,                # one instance: one host per session
            MAX_MESSAGE,      # out buffer
            MAX_MESSAGE,      # in buffer
            0,
            None,
        )
        if self.handle == win32file.INVALID_HANDLE_VALUE:
            raise IpcError(f"CreateNamedPipe({self.name}) failed")
        return self

    def accept(self):
        win32pipe.ConnectNamedPipe(self.handle, None)
        return self

    def recv(self) -> Dict[str, Any]:
        return _recv(self.handle)

    def send(self, payload: Dict[str, Any]) -> None:
        _send(self.handle, payload)

    def close(self):
        if self.handle:
            try:
                win32pipe.DisconnectNamedPipe(self.handle)
            except Exception:
                pass
            try:
                win32file.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None


class PipeClient:
    """Host side: connect to a worker's pipe and issue requests."""

    def __init__(self, session_id: str):
        self.name = pipe_name(session_id)
        self.handle = None
        self._next_id = 0
        # Requests whose response was given up on. Their answers still arrive.
        self._abandoned: set = set()

    def connect(self, timeout: float = 30.0, poll: float = 0.15):
        """Retry until the worker has created its pipe, or time out.

        The worker needs a moment to start Python and import pywinauto, so a
        connect failure is expected for the first second or two.
        """
        deadline = time.time() + timeout
        last: Optional[Exception] = None
        while time.time() < deadline:
            try:
                self.handle = win32file.CreateFile(
                    self.name,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0, None, win32file.OPEN_EXISTING, 0, None,
                )
                win32pipe.SetNamedPipeHandleState(
                    self.handle, win32pipe.PIPE_READMODE_BYTE, None, None
                )
                return self
            except pywintypes.error as exc:
                last = exc
                time.sleep(poll)
        raise IpcError(f"could not connect to {self.name} within {timeout}s: {last}")

    def call(self, method: str, _timeout: Optional[float] = None, /, **params) -> Dict[str, Any]:
        """Issue one request and read *its* response.

        Two things this deliberately does not do, both learned the hard way:

        It does not read "the next message" and assume it belongs to this call.
        It used to, and that made an abandoned call permanently poisonous: when a
        slow call was given up on by the client, its late response was handed to
        the *following* call, and every response after that was off by one - a
        session confidently reporting one control's state as another's. Ids are
        matched, and a response to an earlier, abandoned request is discarded
        here rather than mistaken for this one. That also means the pipe heals
        itself on the next call instead of staying broken.

        And it does not wait forever: `_timeout` is a wall-clock deadline, named
        with an underscore so it cannot collide with a handler parameter (`run`
        already takes a `timeout` of its own).
        """
        if self.handle is None:
            raise IpcError("not connected")
        self._next_id += 1
        request_id = self._next_id
        _send(self.handle, {"id": request_id, "method": method, "params": params})

        deadline = time.time() + _timeout if _timeout else None
        while True:
            try:
                response = _recv(self.handle, deadline)
            except IpcTimeout:
                # The worker is still working; its answer will arrive on the pipe
                # eventually and must be swallowed by a later call rather than
                # read as that call's result.
                self._abandoned.add(request_id)
                raise IpcTimeout(
                    f"{method} did not answer within {_timeout}s. The worker may "
                    "still be running it; retry, or destroy the session if it "
                    "stays wedged."
                ) from None

            response_id = response.get("id")
            if response_id == request_id:
                break
            if response_id in self._abandoned:
                self._abandoned.discard(response_id)
                continue
            if isinstance(response_id, int) and response_id < request_id:
                continue  # a straggler from a call that was given up on
            raise IpcError(
                f"pipe desynchronized: expected a response to request "
                f"{request_id} ({method}), got {response_id}. Destroy this session."
            )

        if not response.get("ok"):
            raise IpcError(
                f"{method} failed in session worker: {response.get('error')}"
            )
        return response.get("result", {})

    def close(self):
        if self.handle:
            try:
                win32file.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None
