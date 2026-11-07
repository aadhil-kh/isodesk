"""Launching and terminating processes on a specific desktop.

Phase 0 spikes established three launch paths with very different behaviour:

  * plain exe            -> CreateProcess honours STARTUPINFO.lpDesktop
  * Store/MSIX app       -> must go through IApplicationActivationManager,
                            called from a process ALREADY on the target desktop
  * shell:AppsFolder\\... -> hands off to the running explorer and opens the app
                            on the host's Default desktop. Never use it.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes
from typing import Any, Dict, List, Optional, Set

import win32process

from .desktop import app_windows, enum_desktop_windows

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


class LaunchError(RuntimeError):
    pass


def launch_on_desktop(
    cmdline: str,
    desktop: str,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    no_window: bool = False,
) -> int:
    """CreateProcess with STARTUPINFO.lpDesktop; returns the new pid.

    :param desktop: full `WinSta0\\<name>` path, from `desktop.desktop_path()`.
    """
    si = win32process.STARTUPINFO()
    si.lpDesktop = desktop
    flags = CREATE_NO_WINDOW if no_window else 0
    try:
        _hproc, _hthread, pid, _tid = win32process.CreateProcess(
            None, cmdline, None, None, False, flags, env, cwd, si
        )
    except Exception as exc:
        raise LaunchError(f"CreateProcess({cmdline!r}) on {desktop}: {exc}") from exc
    return pid


def activate_packaged_app(aumid: str, arguments: str = "") -> int:
    """Activate an MSIX app via IApplicationActivationManager; returns its pid.

    MUST be called from a process already running on the target desktop - the
    activation infrastructure places the app on the caller's desktop. Calling
    this from the host process would put the app on the user's desktop.
    """
    import comtypes
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
    from ctypes import POINTER, c_uint, c_wchar_p

    class IApplicationActivationManager(IUnknown):
        _iid_ = GUID("{2e941141-7f97-4756-ba1d-9decde894a3d}")
        _methods_ = [
            COMMETHOD(
                [], HRESULT, "ActivateApplication",
                (["in"], c_wchar_p, "appUserModelId"),
                (["in"], c_wchar_p, "arguments"),
                (["in"], c_uint, "options"),
                (["out"], POINTER(wintypes.DWORD), "processId"),
            ),
        ]

    comtypes.CoInitialize()
    manager = comtypes.CoCreateInstance(
        GUID("{45BA127D-10A8-46EA-8AB7-56EA9078943C}"),
        interface=IApplicationActivationManager,
        clsctx=comtypes.CLSCTX_LOCAL_SERVER,
    )
    try:
        return manager.ActivateApplication(aumid, arguments or None, 0)
    except Exception as exc:
        raise LaunchError(f"ActivateApplication({aumid!r}): {exc}") from exc


def resolve_aumid(name_fragment: str) -> List[Dict[str, str]]:
    """Look up AUMIDs of installed packaged apps matching a fragment.

    Uses PowerShell's Get-StartApps because there is no stable public Win32 API
    for enumerating start-menu AUMIDs.
    """
    ps = (
        "Get-StartApps | Where-Object { $_.Name -like '*"
        + name_fragment.replace("'", "''")
        + "*' -or $_.AppID -like '*"
        + name_fragment.replace("'", "''")
        + "*' } | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=30,
        )
        import json

        data = json.loads(out.stdout or "[]")
        if isinstance(data, dict):
            data = [data]
        return [{"name": d.get("Name"), "aumid": d.get("AppID")} for d in data]
    except Exception:
        return []


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def module_path_for_pid(pid: int) -> Optional[str]:
    """Full path of a process's main executable.

    Uses QueryFullProcessImageNameW with PROCESS_QUERY_LIMITED_INFORMATION,
    which - unlike GetModuleFileNameEx - succeeds against packaged apps without
    needing PROCESS_VM_READ.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(handle)


def module_info(pid: int) -> Dict[str, Any]:
    """Identify the binary a launched process is actually running.

    A launch response that reports only a pid cannot distinguish "my change is
    live" from "the MSIX package points at a deploy layout a plain build does not
    refresh, so this is a two-week-old payload". That failure is silent - the UI
    simply looks unchanged - and diagnosing it costs far more than reporting the
    path and mtime here.
    """
    import datetime

    info: Dict[str, Any] = {"pid": pid}
    path = module_path_for_pid(pid)
    if not path:
        info["module_path"] = None
        info["note"] = "could not open process to read its image path"
        return info

    info["module_path"] = path
    try:
        stat = os.stat(path)
        info["module_mtime"] = datetime.datetime.fromtimestamp(
            stat.st_mtime
        ).isoformat(timespec="seconds")
        info["module_size"] = stat.st_size
    except OSError as exc:
        info["module_mtime"] = None
        info["note"] = f"cannot stat image: {exc}"

    # For packaged apps the exe lives under
    # C:\Program Files\WindowsApps\<PackageFullName>\..., so the install location
    # falls straight out of the path - no Get-AppxPackage subprocess needed.
    parts = os.path.normpath(path).split(os.sep)
    for index, part in enumerate(parts):
        if part.lower() == "windowsapps" and index + 1 < len(parts):
            info["package_full_name"] = parts[index + 1]
            info["install_location"] = os.sep.join(parts[: index + 2])
            info["packaged"] = True
            break
    else:
        info["packaged"] = False

    return info


def kill_process_tree(pid: int) -> bool:
    result = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True
    )
    return result.returncode == 0


def kill_desktop_processes(hdesk: int, host_hdesk: int, extra_pids: Optional[Set[int]] = None) -> List[int]:
    """Terminate processes belonging to an isolated desktop.

    Safety rule: a pid that also owns a window on the HOST desktop is a host
    process and is never touched, even if it happens to have a window on the
    isolated desktop too. Getting this wrong would kill the user's applications.
    """
    host_pids = {w["pid"] for w in enum_desktop_windows(host_hdesk, visible_only=False)}
    iso_pids = {w["pid"] for w in enum_desktop_windows(hdesk, visible_only=False)}
    iso_pids |= set(extra_pids or ())

    killable = (iso_pids - host_pids) - {os.getpid(), 0}
    killed = []
    for pid in killable:
        if kill_process_tree(pid):
            killed.append(pid)
    return killed
