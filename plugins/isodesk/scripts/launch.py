"""Start the isodesk MCP server from a private, pinned virtual environment.

Claude Code (.mcp.json), Codex and Copilot CLI (mcp.json) run this as the
plugin's stdio MCP server. It needs nothing but a Python 3.10+ on PATH: on
first run it builds a venv in the host's plugin data directory, installs
server/requirements.lock into it, and from then on starts the server from that
venv straight away. The venv is keyed on the lock file and the base
interpreter, so a plugin update that changes a pin, or a new system Python,
gets a fresh venv; an update that only changes isodesk's own code does not,
because the package runs from the plugin directory via PYTHONPATH.

Standard library only, and nothing may reach stdout except the server itself:
stdout is the MCP channel. Everything here writes to stderr or install.log.

    python launch.py              # run the MCP server (what .mcp.json and mcp.json do)
    python launch.py --prepare    # build the venv now, in the foreground, and exit

On a first run the server cannot wait for the install: hosts give it about 30s
to answer, and the install takes about a minute. So the install runs in a
detached process (`--install`) that outlives the launcher, the launcher waits
for it briefly, and if it is not done yet exits with a message saying so. The
next connection attempt finds the venv ready.
"""

from __future__ import annotations

import hashlib
import msvcrt
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager

MIN_PYTHON = (3, 10)
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(PLUGIN_ROOT, "server")
REQUIREMENTS = os.path.join(SERVER_DIR, "requirements.lock")
READY_MARKER = ".isodesk-ready"
LOCK_WAIT_SECONDS = 15 * 60
# Hosts give a stdio server about 30s to answer initialize, and the server
# itself takes a few seconds to come up, so this is all the install can have.
SERVER_WAIT_SECONDS = 20


def log(message: str) -> None:
    print(f"[isodesk] {message}", file=sys.stderr, flush=True)


def data_dir() -> str:
    # The host's per-plugin data dir survives plugin updates. Agent Plugins hosts
    # (Codex, Copilot CLI) set PLUGIN_DATA; Claude Code sets CLAUDE_PLUGIN_DATA.
    # The fallback covers running this by hand, or a host that sets neither.
    base = os.environ.get("PLUGIN_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "isodesk-plugin"
    )
    os.makedirs(base, exist_ok=True)
    return base


def env_key() -> str:
    # Pins only: editing a comment in the lock file must not rebuild every venv.
    digest = hashlib.sha256()
    with open(REQUIREMENTS, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#"):
                digest.update(line.encode() + b"\n")
    digest.update(sys.version.encode())
    digest.update(os.path.normcase(os.path.realpath(sys.base_prefix)).encode())
    return digest.hexdigest()[:12]


def venv_python(venv: str) -> str:
    return os.path.join(venv, "Scripts", "python.exe")


def is_ready(venv: str) -> bool:
    return os.path.isfile(os.path.join(venv, READY_MARKER)) and os.path.isfile(venv_python(venv))


@contextmanager
def install_lock(base: str, wait: bool):
    """Serialise venv builds across processes; released by the OS if we die.

    Yields False instead of waiting when `wait` is off and another process
    already holds the lock.
    """
    handle = open(os.path.join(base, "install.lock"), "a+")
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    waited = False
    acquired = False
    while not acquired:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            acquired = True
        except OSError:
            if not wait:
                break
            if time.monotonic() > deadline:
                handle.close()
                raise SystemExit("[isodesk] timed out waiting for another install to finish")
            if not waited:
                log("another isodesk install is running; waiting for it")
                waited = True
            time.sleep(1)
    try:
        yield acquired
    finally:
        if acquired:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def run_logged(cmd: list, log_path: str) -> None:
    with open(log_path, "a", encoding="utf-8") as out:
        out.write(f"\n$ {subprocess.list2cmdline(cmd)}\n")
        out.flush()
        # stdin is the MCP channel too: pip must not read from it.
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        with open(log_path, encoding="utf-8", errors="replace") as handle:
            tail = handle.read()[-3000:]
        log(f"command failed ({result.returncode}): {subprocess.list2cmdline(cmd)}\n{tail}")
        raise SystemExit(f"[isodesk] dependency install failed; full log at {log_path}")


def rename_with_retry(src: str, dst: str) -> None:
    # A virus scanner still holding a freshly written file makes the first
    # rename fail on Windows; it clears within a few seconds.
    for attempt in range(10):
        try:
            os.rename(src, dst)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(1)


def prune(base: str, keep: str) -> None:
    # Runs only under the install lock, so no other build is in progress here.
    for name in os.listdir(base):
        path = os.path.join(base, name)
        if name.startswith("venv-") and path != keep and os.path.isdir(path):
            # A server from the previous version may still hold files open
            # here; whatever is left goes on a later run.
            shutil.rmtree(path, ignore_errors=True)


def build(base: str, venv: str) -> None:
    """Build the venv; the caller holds the install lock.

    The build happens under a fixed temporary name and is renamed at the end,
    so a venv that exists is always complete. If a build is cut short, the next
    one reuses the half-built venv and pip skips what it already installed.
    """
    log_path = os.path.join(base, "install.log")
    log(f"installing isodesk dependencies into {venv} (log: {log_path})")
    started = time.monotonic()
    staging = f"{venv}.building"
    if not os.path.isfile(venv_python(staging)):
        shutil.rmtree(staging, ignore_errors=True)
        run_logged([sys.executable, "-m", "venv", staging], log_path)
    run_logged(
        [venv_python(staging), "-m", "pip", "install", "--disable-pip-version-check",
         "--no-input", "-r", REQUIREMENTS],
        log_path,
    )
    with open(os.path.join(staging, READY_MARKER), "w", encoding="utf-8") as marker:
        marker.write(f"{sys.version}\n{REQUIREMENTS}\n")
    shutil.rmtree(venv, ignore_errors=True)
    rename_with_retry(staging, venv)
    log(f"dependencies ready in {time.monotonic() - started:.0f}s")
    prune(base, keep=venv)


def install(base: str, venv: str, wait: bool) -> None:
    with install_lock(base, wait) as acquired:
        if acquired and not is_ready(venv):
            build(base, venv)


def start_background_install() -> None:
    """Run the install in its own process, so it outlives this launcher.

    A host that times out waiting for the server kills the launcher; an install
    running inside it would die with it and start over on every retry.
    """
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    cmd = [sys.executable, os.path.abspath(__file__), "--install"]
    io = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    try:
        # Leave the host's job object too, in case it kills the whole tree.
        subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **io)
    except OSError:
        # The job does not allow breakaway, so it does not kill on close either.
        subprocess.Popen(cmd, creationflags=flags, **io)


def main() -> int:
    if sys.platform != "win32":
        log("isodesk drives Windows desktops and runs only on Windows")
        return 1
    if sys.version_info < MIN_PYTHON:
        log(f"isodesk needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ on PATH; "
            f"'python' is {sys.version.split()[0]} at {sys.executable}")
        return 1

    base = data_dir()
    venv = os.path.join(base, f"venv-{env_key()}")
    args = sys.argv[1:]
    if "--install" in args:
        # The background install; exits at once if another one is running.
        install(base, venv, wait=False)
        return 0
    if "--prepare" in args:
        install(base, venv, wait=True)
        log(f"ready: {venv_python(venv)}")
        return 0

    if not is_ready(venv):
        start_background_install()
        deadline = time.monotonic() + SERVER_WAIT_SECONDS
        while not is_ready(venv) and time.monotonic() < deadline:
            time.sleep(0.5)
        if not is_ready(venv):
            log("isodesk is still installing its dependencies (first run, about a minute). "
                "The install carries on in the background: in a minute, reconnect isodesk "
                "by hand (Claude Code: /mcp, select isodesk, Reconnect). A new session does "
                "not retry it until 15 minutes after this failure. Progress and errors: "
                f"{os.path.join(base, 'install.log')}; to install in the foreground, run "
                f"python \"{os.path.abspath(__file__)}\" --prepare")
            return 1

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (SERVER_DIR, env.get("PYTHONPATH")) if p)
    try:
        # stdin and stdout pass straight through to the server.
        return subprocess.run([venv_python(venv), "-m", "isodesk.mcp_server"], env=env, cwd=SERVER_DIR).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
