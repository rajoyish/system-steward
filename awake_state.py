"""Shared Keep Awake session record and process-identity checks.

The record lives in ``~/.system-steward/awake_session.json`` (override the folder with
``SYSTEM_STEWARD_STATE_DIR``). It stores the session PID *and* that process's creation time and
executable, so a PID that the OS has since reused for an unrelated program is never reported or
terminated as ours.
"""
import json
import os
from pathlib import Path
import time
from typing import Any

from platforms import IS_WINDOWS

if IS_WINDOWS:
    from platforms.windows import process_identity as _os_identity
    from platforms.windows import terminate_pid as _os_terminate
else:
    from platforms.darwin import process_identity as _os_identity
    from platforms.darwin import terminate_pid as _os_terminate

STATE_NAME = "awake_session.json"


def state_dir() -> Path:
    override = os.environ.get("SYSTEM_STEWARD_STATE_DIR")
    return Path(override) if override else Path.home() / ".system-steward"


def state_file() -> Path:
    return state_dir() / STATE_NAME


# ---------------------------------------------------------------- process identity

def process_identity(pid: object) -> dict[str, Any] | None:
    """Return {"alive", "create_time", "exe"} for a live PID, or None when it is not running."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    return _os_identity(pid)


def process_alive(pid: object) -> bool:
    return process_identity(pid) is not None


def owned_executable(exe: str | None) -> bool:
    if not exe:
        return False
    name = os.path.basename(str(exe)).lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name == "caffeinate" or name.startswith("python")


def verify_owned(state: object) -> bool:
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

def record(pid: int, **fields: Any) -> dict[str, Any]:
    """Build a session record bound to the identity of ``pid`` as it exists right now."""
    identity = process_identity(pid) or {}
    return {"pid": int(pid), "create_time": identity.get("create_time"), "exe": identity.get("exe"),
            "started": fields.pop("started", time.time()), **fields}


def save(state: dict[str, Any]) -> bool:
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


def load_raw() -> dict[str, Any] | None:
    try:
        with open(state_file(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def remove(only_if_pid: int | None = None) -> None:
    try:
        if only_if_pid is not None:
            data = load_raw()
            if data is not None and data.get("pid") != only_if_pid:
                return
        state_file().unlink(missing_ok=True)
    except OSError:
        pass


def read_owned() -> dict[str, Any] | None:
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


def terminate(state: dict[str, Any]) -> bool:
    """Terminate the recorded process only after re-verifying it is ours. Returns True if signalled."""
    if not verify_owned(state):
        return False
    return _os_terminate(int(state["pid"]))
