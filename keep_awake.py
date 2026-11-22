"""Own and stop a macOS caffeinate session without changing power settings."""
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


@dataclass(frozen=True)
class AwakePlan:
    argv: tuple[str, ...]
    mode: str
    seconds: int | None
    cwd: Path

    @property
    def command(self):
        return shlex.join(self.argv)


def positive_integer(value, label, maximum):
    value = value.strip()
    if not value.isascii() or not value.isdecimal() or not 0 < int(value) <= maximum:
        raise ValueError(f"{label} must be a whole number from 1 to {maximum}.")
    return int(value)


def plan_session(flags=("i",), mode="manual", duration="", pid="", command="", cwd=None):
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
    return AwakePlan(tuple(argv), mode, seconds, directory)


def available():
    return sys.platform == "darwin" and os.access("/usr/bin/caffeinate", os.X_OK)


class AwakeSession:
    def __init__(self):
        self.process = None
        self.plan = None
        self.started = None
        self.stopped = False
        self.output = deque(maxlen=16)

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    @property
    def remaining(self):
        if self.running and self.plan.seconds is not None:
            return max(0, self.plan.seconds - int(time.monotonic() - self.started))
        return None

    def start(self, plan):
        if not available():
            raise RuntimeError("Keep Awake requires macOS and /usr/bin/caffeinate.")
        if self.running:
            raise RuntimeError("Stop the current Keep Awake session first.")
        if plan.mode == "process":
            try:
                os.kill(int(plan.argv[-1]), 0)
            except (ProcessLookupError, PermissionError) as exc:
                raise ValueError("The watched PID is unavailable or cannot be inspected.") from exc
        process = subprocess.Popen(plan.argv, cwd=plan.cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        self.process, self.plan = process, plan
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
        if not self.running:
            return
        process = self.process
        self.stopped = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
        # A utility can exit on TERM while a child ignores it. Remove remaining
        # members of our private group, including caffeinate's monitor helper.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def status(self):
        if self.process is None:
            return "No Keep Awake session running."
        if self.running:
            remaining = self.remaining
            limit = f"{remaining}s remaining" if remaining is not None else {
                "manual": "until stopped", "process": "until the watched PID exits", "command": "until the command exits"
            }[self.plan.mode]
            return f"Keep Awake active · PID {self.process.pid} · {limit}"
        if self.stopped:
            return "Keep Awake stopped."
        code = self.process.returncode
        return "Keep Awake finished." if code == 0 else f"Keep Awake ended · exit code {code}."

    def output_tail(self):
        return b"".join(tuple(self.output)).decode("utf-8", errors="replace")
