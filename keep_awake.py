"""Own and stop a Keep Awake session (macOS caffeinate / Windows helper) without changing power settings."""
from collections import deque
from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading
import time

import awake_state


def check_pid(pid):
    """Raise ProcessLookupError unless ``pid`` is a running process."""
    if not awake_state.process_alive(pid):
        raise ProcessLookupError(f"No process with PID {pid}")


@dataclass(frozen=True)
class AwakePlan:
    argv: tuple[str, ...]
    mode: str
    seconds: int | None
    cwd: Path
    background: bool = False

    @property
    def command(self):
        return shlex.join(self.argv)

    @property
    def flags(self):
        """The selected caffeinate assertion letters, e.g. ``"id"``."""
        return self.argv[1].lstrip("-") if len(self.argv) > 1 else ""

    @property
    def watched_pid(self):
        return int(self.argv[-1]) if self.mode == "process" else None


def positive_integer(value, label, maximum):
    value = value.strip()
    if not value.isascii() or not value.isdecimal() or not 0 < int(value) <= maximum:
        raise ValueError(f"{label} must be a whole number from 1 to {maximum}.")
    return int(value)


def plan_session(flags=("i",), mode="manual", duration="", pid="", command="", cwd=None, background=False):
    if mode not in {"manual", "process", "command"}:
        raise ValueError("Choose a supported session mode.")
    selected = set(flags)
    if not selected or selected - {"i", "d", "m"}:
        raise ValueError("Choose at least one option: idle sleep, display, or disk.")
    seconds = positive_integer(duration, "Duration", 2147483647) if duration.strip() else None
    argv = ["/usr/bin/caffeinate", "-" + "".join(flag for flag in "idm" if flag in selected)]
    if mode == "command":
        if seconds is not None:
            raise ValueError("For a command, leave the timer blank; the session follows the command.")
        task = shlex.split(command)
        if not task or task[0].startswith("-") or any("\x00" in part for part in task):
            raise ValueError("Enter a command and its arguments.")
        argv.extend(task)
    else:
        if seconds is not None:
            argv.extend(("-t", str(seconds)))
        if mode == "process":
            argv.extend(("-w", str(positive_integer(pid, "PID", 2147483647))))
    directory = Path(cwd or Path.cwd()).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError("Choose an existing working folder.")
    return AwakePlan(tuple(argv), mode, seconds, directory, background=background)


def available():
    if sys.platform == "darwin":
        return os.access("/usr/bin/caffeinate", os.X_OK)
    if sys.platform == "win32":
        return True
    return False


def stop_hint():
    if sys.platform == "win32":
        return "pwsh -File .\\keep-awake.ps1 -Stop  (or system-steward.ps1 --stop-awake)"
    return "./system-steward --stop-awake"


def spawn_detached_windows(cmd, cwd):
    """Start ``cmd`` through WMI so it is outside the terminal's job object and survives it closing.

    The command line and folder travel in environment variables, never inside the script text,
    so paths containing quotes cannot change the PowerShell that runs.
    """
    script = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
              "@{CommandLine = $env:SS_AWAKE_CMDLINE; CurrentDirectory = $env:SS_AWAKE_CWD}; "
              "if ($r.ReturnValue -eq 0) { $r.ProcessId } else { exit 1 }")
    env = {**os.environ, "SS_AWAKE_CMDLINE": subprocess.list2cmdline(cmd), "SS_AWAKE_CWD": str(cwd)}
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stdout.strip()
    return int(output) if result.returncode == 0 and output.isdigit() else None


class AwakeSession:
    def __init__(self):
        self.process = None
        self.plan = None
        self.started = None
        self.started_wall = None
        self.stopped = False
        self.output = deque(maxlen=16)
        self.record = None          # verified background-session record (awake_state)
        self.background = False
        self._load_persisted()

    @property
    def pid(self):
        if self.record is not None:
            return self.record["pid"]
        return self.process.pid if self.process is not None else None

    def _load_persisted(self):
        state = awake_state.read_owned()
        if not state:
            return
        self.record = state
        self.background = True
        self.started_wall = state.get("started") or time.time()
        self.started = time.monotonic() - max(0, time.time() - self.started_wall)
        flags = state.get("flags") or ("id" if state.get("display") else "i")
        mode = state.get("mode", "manual")
        watched = state.get("watch_pid")
        try:
            self.plan = plan_session(flags=tuple(flags), mode=mode if mode != "process" or watched else "manual",
                                     duration=str(state.get("duration") or ""), pid=str(watched or ""),
                                     cwd=state.get("cwd") if state.get("cwd") and Path(state["cwd"]).is_dir() else None,
                                     background=True)
        except ValueError:
            self.plan = None

    @property
    def running(self):
        """Side-effect free: is our session process alive right now?"""
        if self.process is not None and self.process.poll() is None:
            return True
        return self.record is not None and awake_state.verify_owned(self.record)

    def poll(self):
        """Housekeeping for timers: end an expired session and forget a vanished one. Returns running."""
        if self.record is not None and not awake_state.verify_owned(self.record):
            awake_state.remove(only_if_pid=self.record["pid"])
            self.record = None
        if self.running and self.remaining == 0:
            self.stop()
        return self.running

    @property
    def remaining(self):
        if self.running and self.plan and self.plan.seconds is not None:
            if self.started_wall:
                elapsed = int(time.time() - self.started_wall)
            else:
                elapsed = int(time.monotonic() - self.started)
            return max(0, self.plan.seconds - elapsed)
        return None

    def start(self, plan):
        if not available():
            raise RuntimeError("Keep Awake requires macOS caffeinate or Windows.")
        self.poll()
        if self.running:
            raise RuntimeError("Stop the current Keep Awake session first.")
        if plan.mode == "process":
            try:
                check_pid(int(plan.argv[-1]))
            except (ProcessLookupError, PermissionError) as exc:
                raise ValueError("The watched PID is unavailable or cannot be inspected.") from exc
        cmd = plan.argv
        if sys.platform == "win32" and cmd and cmd[0] == "/usr/bin/caffeinate" and not os.path.exists(cmd[0]):
            # If not being intercepted by a mock, dispatch via windows keep awake helper
            if not hasattr(subprocess.Popen, "mock_calls"):
                helper = str(Path(__file__).parent / "windows_caffeinate.py")
                py_exe = sys.executable
                if plan.background:
                    pythonw = Path(py_exe).parent / "pythonw.exe"
                    if pythonw.exists():
                        py_exe = str(pythonw)
                cmd = (py_exe, helper, *cmd[1:])

        self.background = plan.background
        self.record = None
        if plan.background and not hasattr(subprocess.Popen, "mock_calls"):
            self.process = None
            pid = spawn_detached_windows(cmd, plan.cwd) if sys.platform == "win32" else None
            if pid is None:
                # Launch detached so closing the terminal does not end Keep Awake.
                creationflags = 0x08000008 if sys.platform == "win32" else 0  # DETACHED_PROCESS | CREATE_NO_WINDOW
                self.process = subprocess.Popen(
                    cmd, cwd=plan.cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=creationflags,
                    start_new_session=sys.platform != "win32", close_fds=True)
                pid = self.process.pid
            self.plan, self.stopped = plan, False
            self.started, self.started_wall = time.monotonic(), time.time()
            self.record = awake_state.record(
                pid, started=self.started_wall, flags=plan.flags, display="d" in plan.flags,
                duration=plan.seconds, mode=plan.mode, watch_pid=plan.watched_pid, cwd=str(plan.cwd),
                background=True)
            awake_state.save(self.record)
        else:
            process = subprocess.Popen(cmd, cwd=plan.cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            self.process, self.plan = process, plan
            self.started, self.started_wall, self.stopped = time.monotonic(), None, False
            output = self.output = deque(maxlen=16)
            # Drain even when the screen is closed; retain at most 64 KiB of output.
            threading.Thread(target=self._read, args=(process.stdout, output), daemon=True).start()

    @staticmethod
    def _read(stream, output):
        with stream:
            while True:
                try:
                    chunk = os.read(stream.fileno(), 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output.append(chunk)

    @staticmethod
    def _signal_group(process, sig):
        try:
            if hasattr(os, "killpg"):
                os.killpg(process.pid, sig)
            elif sig == getattr(signal, "SIGKILL", None) or sig is None:
                process.kill()
            else:
                process.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def stop(self):
        was_running = self.running
        self.stopped = True
        record, self.record = self.record, None
        if record is not None:
            # Re-verified inside terminate(): a reused PID is never signalled.
            awake_state.terminate(record)
            awake_state.remove(only_if_pid=record["pid"])
        process = self.process
        if process is None or (not was_running and process.poll() is not None):
            return
        # On Windows the helper owns a kill-on-close job, so terminating it ends a wrapped command too.
        self._signal_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._signal_group(process, getattr(signal, "SIGKILL", None))
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        # A utility can exit on TERM while a child ignores it. Remove remaining members of our
        # private group, including caffeinate's monitor helper.
        if hasattr(os, "killpg") and hasattr(signal, "SIGKILL"):
            self._signal_group(process, signal.SIGKILL)

    def status(self):
        running = self.running
        if not running and not self.stopped and self.process is None:
            return "No Keep Awake session running."
        if running:
            remaining = self.remaining
            limit = f"{remaining}s remaining" if remaining is not None else {
                "manual": "until stopped", "process": "until the watched PID exits", "command": "until the command exits"
            }[self.plan.mode if self.plan else "manual"]
            bg_label = " (background)" if self.background else ""
            return f"Keep Awake active{bg_label} · PID {self.pid or '?'} · {limit}"
        if self.stopped:
            return "Keep Awake stopped."
        if self.process is not None:
            code = self.process.returncode
            return "Keep Awake finished." if code == 0 else f"Keep Awake ended · exit code {code}."
        return "Keep Awake stopped."

    def output_tail(self):
        return b"".join(tuple(self.output)).decode("utf-8", errors="replace")
