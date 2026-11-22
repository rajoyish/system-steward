"""Windows Keep Awake helper mimicking macOS caffeinate using SetThreadExecutionState.

Usage (caffeinate-compatible subset):
    windows_caffeinate.py [-idmsu] [-t SECONDS] [-w PID] [-b] [--] [command [args...]]
    windows_caffeinate.py --status | --stop
"""
import ctypes
from ctypes import wintypes
import os
import sys
import time

import awake_state

# subprocess / keep_awake are imported only by the code paths that need them, so a background
# session that just holds the sleep assertion stays a minimal interpreter.

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_AWAYMODE_REQUIRED = 0x00000040

DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

# caffeinate assertion letters; only "d" changes behavior here.
FLAG_LETTERS = frozenset("idmsu")


def is_flag_cluster(arg):
    return len(arg) > 1 and arg[0] == "-" and set(arg[1:]) <= FLAG_LETTERS


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


if sys.platform == "win32":
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetThreadExecutionState.argtypes = (wintypes.DWORD,)
    kernel32.SetThreadExecutionState.restype = wintypes.DWORD
    kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), wintypes.DWORD)
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL


def get_process_memory_mb(pid):
    """Private (committed) memory of ``pid`` in MiB: the honest footprint, unlike the trimmable working set."""
    try:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, int(pid))
    except (NameError, TypeError, ValueError, OSError):
        return None
    if not handle:
        return None
    try:
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return round(counters.PrivateUsage / (1024 * 1024), 1)
        return None
    finally:
        kernel32.CloseHandle(handle)


def set_awake_state(display=False):
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    if display:
        flags |= ES_DISPLAY_REQUIRED
    try:
        kernel32.SetThreadExecutionState(flags)
    except (NameError, OSError):
        pass


def clear_awake_state():
    try:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except (NameError, OSError):
        pass


def is_pid_alive(pid):
    return awake_state.process_alive(pid)


def wait_for_pid(pid, duration=None):
    """Block until ``pid`` exits or ``duration`` seconds pass, without polling process state.

    Waits on the process handle in 1 s slices only so Ctrl+C stays responsive in a console.
    """
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not handle:
        return
    deadline = None if duration is None else time.monotonic() + duration
    try:
        while True:
            slice_ms = 1000 if deadline is None else max(0, min(1000, int((deadline - time.monotonic()) * 1000)))
            if kernel32.WaitForSingleObject(handle, slice_ms) != WAIT_TIMEOUT:
                return  # process exited (or the wait failed)
            if deadline is not None and time.monotonic() >= deadline:
                return
    finally:
        kernel32.CloseHandle(handle)


def kill_on_close_job():
    """Put this helper in a job that kills every member when the helper dies.

    Children spawned afterwards inherit the job, so terminating the helper (Stop session, app exit,
    TerminateProcess) also ends a wrapped command and its descendants instead of orphaning them.
    Returns the job handle, which must stay open for the helper's lifetime, or None on failure.
    """
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if (kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                         ctypes.byref(info), ctypes.sizeof(info))
            and kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())):
        return job
    print(f"windows_caffeinate: job object unavailable (error {ctypes.get_last_error()}); "
          "a wrapped command may outlive this helper if it is terminated.", file=sys.stderr)
    kernel32.CloseHandle(job)
    return None


def parse_args(args):
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


def session_mode(options):
    return "process" if options["pid"] else ("command" if options["command"] else "manual")


def flags_of(options):
    return "id" if options["display"] else "i"


def show_status():
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


def stop_session():
    state = awake_state.read_owned()
    if state and awake_state.terminate(state):
        awake_state.remove(only_if_pid=state["pid"])
        print(f"Keep Awake stopped (PID: {state['pid']}).")
    else:
        awake_state.remove()
        print("No Keep Awake session running.")
    return 0


def launch_background(options):
    """Start a detached copy of this helper. The launcher is the only writer of the session record."""
    import subprocess
    from keep_awake import helper_python, spawn_detached_windows

    existing = awake_state.read_owned()
    if existing:
        mem = get_process_memory_mb(existing["pid"])
        mem_str = f" ({mem} MB private memory)" if mem else ""
        print(f"Keep Awake is already running in background (PID: {existing['pid']}{mem_str}).")
        return 0
    cmd = [helper_python(windowless=True), os.path.abspath(__file__), *options["forward"], "--daemon"]
    # WMI creation keeps the session outside the terminal's job, so closing the terminal cannot end it.
    pid = spawn_detached_windows(cmd, os.getcwd())
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
    return 0


def main(argv=None):
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
    set_awake_state(options["display"])

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
