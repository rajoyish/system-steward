"""Shared Keep Awake session record and process-identity checks.

The record lives in ``~/.system-steward/awake_session.json`` (override the folder with
``SYSTEM_STEWARD_STATE_DIR``). It stores the session PID *and* that process's creation time and
executable, so a PID that the OS has since reused for an unrelated program is never reported or
terminated as ours.
"""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

STATE_NAME = "awake_session.json"
OWNED_EXECUTABLES = ("python", "pythonw", "caffeinate")

if sys.platform == "win32":
    from ctypes import wintypes

    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD))
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateProcess.restype = wintypes.BOOL


def state_dir():
    override = os.environ.get("SYSTEM_STEWARD_STATE_DIR")
    return Path(override) if override else Path.home() / ".system-steward"


def state_file():
    return state_dir() / STATE_NAME


# ---------------------------------------------------------------- process identity

def _windows_identity(pid):
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        if ctypes.get_last_error() == ERROR_ACCESS_DENIED:
            # Alive, but not inspectable by this user, so it cannot be our session.
            return {"alive": True, "create_time": None, "exe": None}
        return None
    try:
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        create_time = None
        if _kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                     ctypes.byref(kernel), ctypes.byref(user)):
            create_time = (created.dwHighDateTime << 32) | created.dwLowDateTime
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        exe = buffer.value if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else None
        return {"alive": True, "create_time": create_time, "exe": exe}
    finally:
        _kernel32.CloseHandle(handle)


def _posix_identity(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return {"alive": True, "create_time": None, "exe": None}
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "comm="],
                                capture_output=True, text=True, timeout=3, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return {"alive": True, "create_time": None, "exe": None}
    parts = result.stdout.split(None, 5)
    if result.returncode or len(parts) < 6:
        return None
    return {"alive": True, "create_time": " ".join(parts[:5]), "exe": parts[5].strip()}


def process_identity(pid):
    """Return {"alive", "create_time", "exe"} for a live PID, or None when it is not running."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    return _windows_identity(pid) if sys.platform == "win32" else _posix_identity(pid)


def process_alive(pid):
    return process_identity(pid) is not None


def owned_executable(exe):
    if not exe:
        return False
    name = os.path.basename(str(exe)).lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name == "caffeinate" or name.startswith("python")


def verify_owned(state):
    """True only if the recorded PID is alive and is still the process we launched."""
    if not isinstance(state, dict):
        return False
    identity = process_identity(state.get("pid"))
    if identity is None or not owned_executable(identity["exe"]):
        return False
    recorded = state.get("create_time")
    if recorded is None:
        # Legacy record written before identities were stored: accept only our executables.
        return True
    return identity["create_time"] == recorded


# ---------------------------------------------------------------- record storage

def record(pid, **fields):
    """Build a session record bound to the identity of ``pid`` as it exists right now."""
    identity = process_identity(pid) or {}
    return {"pid": int(pid), "create_time": identity.get("create_time"), "exe": identity.get("exe"),
            "started": fields.pop("started", time.time()), **fields}


def save(state):
    """Atomically write the record; readers never see a partial file."""
    try:
        folder = state_dir()
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / f".{STATE_NAME}.{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(temporary, folder / STATE_NAME)
        return True
    except OSError:
        return False


def load_raw():
    try:
        with open(state_file(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def remove(only_if_pid=None):
    try:
        if only_if_pid is not None:
            data = load_raw()
            if data is not None and data.get("pid") != only_if_pid:
                return
        state_file().unlink(missing_ok=True)
    except OSError:
        pass


def read_owned():
    """Return the live, verified record, deleting a stale or foreign one."""
    data = load_raw()
    if data is None:
        if state_file().exists():
            remove()
        return None
    if verify_owned(data):
        return data
    remove()
    return None


def terminate(state):
    """Terminate the recorded process only after re-verifying it is ours. Returns True if signalled."""
    if not verify_owned(state):
        return False
    pid = int(state["pid"])
    if sys.platform == "win32":
        handle = _kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not handle:
            return False
        try:
            return bool(_kernel32.TerminateProcess(handle, 1))
        finally:
            _kernel32.CloseHandle(handle)
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False
