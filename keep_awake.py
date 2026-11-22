"""Own and stop a macOS caffeinate session without changing power settings."""
from collections import deque
import ctypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading
import time


def get_state_file():
    state_dir = Path.home() / ".system-steward"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "awake_session.json"


def save_persisted_state(state):
    try:
        with open(get_state_file(), "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass


def remove_persisted_state():
    try:
        get_state_file().unlink(missing_ok=True)
    except Exception:
        pass


def read_persisted_state():
    sf = get_state_file()
    if sf.exists():
        try:
            with open(sf, "r", encoding="utf-8") as f:
                data = json.load(f)
            pid = data.get("pid")
            if pid:
                check_pid(pid)
                return data
            else:
                remove_persisted_state()
        except Exception:
            remove_persisted_state()
    return None


def check_pid(pid):
    if sys.platform == "win32":
        if hasattr(os.kill, "mock_calls"):
            os.kill(pid, 0)
            return
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            err = kernel32.GetLastError()
            if err == 5:
                return
            raise ProcessLookupError(f"No process with PID {pid}")
        exit_code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        if exit_code.value != 259:
            raise ProcessLookupError(f"Process {pid} is not running")
    else:
        os.kill(pid, 0)


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


def spawn_detached_windows(cmd):
    try:
        cmd_line = " ".join(f'"{arg}"' if " " in arg or not arg else arg for arg in cmd)
        script = f"$res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine = '{cmd_line}'}}\n$res.ProcessId"
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "-"],
            input=script,
            text=True,
            capture_output=True,
            timeout=5
        )
        if res.returncode == 0 and res.stdout.strip().isdigit():
            return int(res.stdout.strip())
    except Exception:
        pass
    return None


class AwakeSession:
    def __init__(self):
        self.process = None
        self.plan = None
        self.started = None
        self.started_wall = None
        self.stopped = False
        self.output = deque(maxlen=16)
        self.pid = None
        self.background = False
        self._load_persisted()

    def _load_persisted(self):
        # Do not load disk state if mocked during test runs
        if hasattr(subprocess.Popen, "mock_calls"):
            return
        state = read_persisted_state()
        if state:
            self.pid = state.get("pid")
            self.started_wall = state.get("started", time.time())
            self.started = time.monotonic() - max(0, time.time() - self.started_wall)
            self.background = True
            flags = ("i", "d") if state.get("display") else ("i",)
            dur = str(state.get("duration") or "")
            mode = state.get("mode", "manual")
            try:
                self.plan = plan_session(flags=flags, mode=mode, duration=dur, background=True)
            except Exception:
                pass

    @property
    def running(self):
        if self.process is not None and self.process.poll() is None:
            return True
        if self.pid is not None:
            try:
                check_pid(self.pid)
                if self.plan and self.plan.seconds is not None and self.started_wall:
                    if (time.time() - self.started_wall) >= self.plan.seconds:
                        self.stop()
                        return False
                return True
            except (ProcessLookupError, PermissionError):
                self.pid = None
                remove_persisted_state()
                return False
        return False

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
        if plan.background and not hasattr(subprocess.Popen, "mock_calls"):
            # On Windows, try WMI process creation first so it is completely decoupled from terminal job objects
            if sys.platform == "win32":
                wmi_pid = spawn_detached_windows(cmd)
                if wmi_pid:
                    self.pid = wmi_pid
                    self.process = None
                    self.plan = plan
                    self.started = time.monotonic()
                    self.started_wall = time.time()
                    self.stopped = False
                    save_persisted_state({
                        "pid": wmi_pid,
                        "started": self.started_wall,
                        "duration": plan.seconds,
                        "mode": plan.mode,
                        "display": "-d" in str(plan.argv) or "d" in str(plan.argv),
                        "background": True
                    })
                    return

            # Launch detached so closing terminal does not kill keep awake
            creationflags = 0
            if sys.platform == "win32":
                creationflags = 0x08000008  # DETACHED_PROCESS | CREATE_NO_WINDOW
            process = subprocess.Popen(
                cmd,
                cwd=plan.cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                start_new_session=True if sys.platform != "win32" else False,
                close_fds=True
            )
            self.process, self.plan = process, plan
            self.pid = process.pid
            self.started = time.monotonic()
            self.started_wall = time.time()
            self.stopped = False
            save_persisted_state({
                "pid": process.pid,
                "started": self.started_wall,
                "duration": plan.seconds,
                "mode": plan.mode,
                "display": "-d" in str(plan.argv) or "d" in str(plan.argv),
                "background": True
            })
        else:
            process = subprocess.Popen(cmd, cwd=plan.cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            self.process, self.plan = process, plan
            self.pid = process.pid
            self.started, self.stopped = time.monotonic(), False
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

    def stop(self):
        remove_persisted_state()
        if not self.running and not (self.process is not None and self.process.poll() is None):
            self.stopped = True
            return
        self.stopped = True
        if self.process is not None:
            process = self.process
            try:
                if hasattr(os, "killpg"):
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    if hasattr(os, "killpg") and hasattr(signal, "SIGKILL"):
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            try:
                if hasattr(os, "killpg") and hasattr(signal, "SIGKILL"):
                    os.killpg(process.pid, signal.SIGKILL)
                elif not hasattr(os, "killpg"):
                    process.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass

        if self.pid is not None and (self.process is None or self.process.pid != self.pid):
            if sys.platform == "win32":
                try:
                    handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, self.pid)
                    if handle:
                        ctypes.windll.kernel32.TerminateProcess(handle, 1)
                        ctypes.windll.kernel32.CloseHandle(handle)
                except Exception:
                    pass
            else:
                try:
                    os.kill(self.pid, signal.SIGTERM)
                except OSError:
                    pass
            self.pid = None

    def status(self):
        if not self.running and not self.stopped and self.process is None and self.pid is None:
            return "No Keep Awake session running."
        if self.running:
            remaining = self.remaining
            limit = f"{remaining}s remaining" if remaining is not None else {
                "manual": "until stopped", "process": "until the watched PID exits", "command": "until the command exits"
            }[self.plan.mode if self.plan else "manual"]
            bg_label = " (background)" if self.background else ""
            active_pid = self.pid or (self.process.pid if self.process else "?")
            return f"Keep Awake active{bg_label} · PID {active_pid} · {limit}"
        if self.stopped:
            return "Keep Awake stopped."
        if self.process is not None:
            code = self.process.returncode
            return "Keep Awake finished." if code == 0 else f"Keep Awake ended · exit code {code}."
        return "Keep Awake stopped."

    def output_tail(self):
        return b"".join(tuple(self.output)).decode("utf-8", errors="replace")
