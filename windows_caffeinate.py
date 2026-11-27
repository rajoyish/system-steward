"""Windows Keep Awake helper mimicking macOS caffeinate using SetThreadExecutionState.

Usage (caffeinate-compatible subset):
    windows_caffeinate.py [-idmsu] [-t SECONDS] [-w PID] [-b] [--] [command [args...]]
    windows_caffeinate.py --status | --stop

All Win32 calls live in platforms.windows. subprocess / keep_awake are imported only by the code
paths that need them, so a background session that just holds the sleep request stays minimal.
"""
import os
import sys
import time
from typing import Any

import awake_state
from platforms import windows as win

DETACHED_PROCESS = win.DETACHED_PROCESS
CREATE_NO_WINDOW = win.CREATE_NO_WINDOW

# caffeinate assertion letters; only "d" changes behavior here.
FLAG_LETTERS = frozenset("idmsu")


def is_flag_cluster(arg: str) -> bool:
    return len(arg) > 1 and arg[0] == "-" and set(arg[1:]) <= FLAG_LETTERS


def get_process_memory_mb(pid: int) -> float | None:
    """Private (committed) memory of ``pid`` in MiB."""
    return win.private_memory_mb(pid)


def set_awake_state(display: bool = False) -> None:
    win.set_execution_state(display=display)


def clear_awake_state() -> None:
    win.set_execution_state(clear=True)


def is_pid_alive(pid: int) -> bool:
    return awake_state.process_alive(pid)


def wait_for_pid(pid: int, duration: float | None = None) -> None:
    win.wait_for_process(pid, duration)


def kill_on_close_job() -> int | None:
    """Kill-on-close job for this helper so a wrapped command never outlives it (see platforms.windows)."""
    job, error = win.kill_on_close_job()
    if job is None:
        print(f"windows_caffeinate: job object unavailable (error {error}); "
              "a wrapped command may outlive this helper if it is terminated.", file=sys.stderr)
    return job


def parse_args(args: list[str]) -> dict[str, Any]:
    """Parse a caffeinate-style command line. The wrapped command starts at the first non-option or after ``--``."""
    options = {"display": False, "duration": None, "pid": None, "command": [], "background": False,
               "daemon": False, "forward": []}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-b", "--background"):
            options["background"] = True
        elif arg == "--daemon":
            options["daemon"] = True
        elif arg == "--":
            options["command"] = args[i + 1:]
            options["forward"].extend(args[i:])
            break
        elif arg in ("-t", "-w"):
            if i + 1 >= len(args):
                raise ValueError(f"{arg} needs a value")
            value = args[i + 1]
            try:
                if arg == "-t":
                    options["duration"] = float(value)
                else:
                    options["pid"] = int(value)
            except ValueError as exc:
                raise ValueError(f"invalid value for {arg}: {value}") from exc
            options["forward"].extend((arg, value))
            i += 1
        elif is_flag_cluster(arg):
            options["display"] = options["display"] or "d" in arg
            options["forward"].append(arg)
        elif arg.startswith("-"):
            raise ValueError(f"unknown option: {arg}")
        else:
            options["command"] = args[i:]
            options["forward"].extend(args[i:])
            break
        i += 1
    return options


def session_mode(options: dict[str, Any]) -> str:
    return "process" if options["pid"] else ("command" if options["command"] else "manual")


def flags_of(options: dict[str, Any]) -> str:
    return "id" if options["display"] else "i"


def show_status() -> int:
    state = awake_state.read_owned()
    if not state:
        print("No Keep Awake session running.")
        return 1
    pid = state.get("pid")
    mem = get_process_memory_mb(pid)
    mem_str = f" · {mem} MB private memory" if mem is not None else ""
    disp = " · Display kept awake" if state.get("display") else " · Display can sleep"
    remaining_str = ""
    if state.get("duration") and state.get("started"):
        remaining_str = f" · {max(0, int(state['duration'] - (time.time() - state['started'])))}s remaining"
    print(f"Keep Awake active in background (PID: {pid}{mem_str}{disp}{remaining_str})")
    return 0


def stop_session() -> int:
    state = awake_state.read_owned()
    if state and awake_state.terminate(state):
        awake_state.remove(only_if_pid=state["pid"])
        print(f"Keep Awake stopped (PID: {state['pid']}).")
    else:
        awake_state.remove()
        print("No Keep Awake session running.")
    return 0


def launch_background(options: dict[str, Any]) -> int:
    """Start a detached copy of this helper. The launcher is the only writer of the session record."""
    import subprocess

    from keep_awake import helper_python

    existing = awake_state.read_owned()
    if existing:
        mem = get_process_memory_mb(existing["pid"])
        mem_str = f" ({mem} MB private memory)" if mem else ""
        print(f"Keep Awake is already running in background (PID: {existing['pid']}{mem_str}).")
        return 0
    cmd = [helper_python(windowless=True), os.path.abspath(__file__), *options["forward"], "--daemon"]
    # WMI creation keeps the session outside the terminal's job, so closing the terminal cannot end it.
    pid = win.spawn_detached(cmd, os.getcwd())
    if pid is None:
        pid = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW, close_fds=True).pid
    record = awake_state.record(
        pid, flags=flags_of(options), display=options["display"], duration=options["duration"],
        mode=session_mode(options), watch_pid=options["pid"], cwd=os.getcwd(), background=True)
    if record["create_time"] is None or not awake_state.save(record):
        print("windows_caffeinate: the background session did not start.", file=sys.stderr)
        return 1
    disp_msg = "System & display sleep prevented." if options["display"] else "Idle system sleep prevented (display can sleep)."
    print(f"✓ Keep Awake started in background (PID: {pid}).")
    print(f"  Mode: {disp_msg}")
    print("  Persistence: Keeps running after this terminal closes; ends when the computer shuts down.")
    print("  Commands: pwsh -File .\\keep-awake.ps1 -Stop  (or -Status)")
    if not options["display"] and win.modern_standby():
        print(f"  Warning: {win.MODERN_STANDBY_WARNING}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)

    # Control commands are only recognised on their own, never inside a wrapped command line.
    if args == ["--status"]:
        return show_status()
    if args == ["--stop"]:
        return stop_session()

    try:
        options = parse_args(args)
    except ValueError as exc:
        print(f"windows_caffeinate: {exc}", file=sys.stderr)
        return 2

    if options["background"] and not options["daemon"]:
        return launch_background(options)

    job = kill_on_close_job() if options["command"] else None  # noqa: F841 - kept open for our lifetime
    duration, pid = options["duration"], options["pid"]
    # ES_CONTINUOUS keeps the request in force for this thread's lifetime; no refresh loop is needed.
    try:
        set_awake_state(options["display"])
    except OSError as exc:
        print(f"windows_caffeinate: {exc}", file=sys.stderr)
        if options["daemon"]:
            awake_state.remove(only_if_pid=os.getpid())
        return 1
    if not options["display"] and not options["daemon"] and win.modern_standby():
        print(f"windows_caffeinate: {win.MODERN_STANDBY_WARNING}", file=sys.stderr)

    try:
        if options["command"]:
            import subprocess
            try:
                proc = subprocess.Popen(options["command"])
            except OSError as exc:
                print(f"windows_caffeinate: cannot run {options['command'][0]}: {exc}", file=sys.stderr)
                return 127
            return proc.wait()
        if pid:
            wait_for_pid(pid, duration)
        elif duration is not None:
            time.sleep(max(0.0, duration))  # interruptible by Ctrl+C
        else:
            while True:
                time.sleep(86400)
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        clear_awake_state()
        if options["daemon"]:
            # The launcher wrote the record; remove it on a clean exit (TerminateProcess skips this,
            # and readers discard the stale record because the PID is no longer ours).
            awake_state.remove(only_if_pid=os.getpid())


if __name__ == "__main__":
    sys.exit(main())
