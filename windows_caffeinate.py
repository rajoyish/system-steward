"""Windows Keep Awake helper mimicking macOS caffeinate using SetThreadExecutionState.

Usage (caffeinate-compatible subset):
    windows_caffeinate.py [-idmsu] [-t SECONDS] [-w PID] [-b] [--] [command [args...]]
    windows_caffeinate.py --status | --stop
"""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import awake_state

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_AWAYMODE_REQUIRED = 0x00000040

DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

# caffeinate assertion letters; only "d" changes behavior here.
FLAG_CLUSTER = re.compile(r"-[idmsu]+\Z")


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
    psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), wintypes.DWORD)
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    psapi.EmptyWorkingSet.argtypes = (wintypes.HANDLE,)
    psapi.EmptyWorkingSet.restype = wintypes.BOOL


def trim_memory():
    """Trim unused pages from the working set."""
    try:
        psapi.EmptyWorkingSet(kernel32.GetCurrentProcess())
    except (NameError, OSError):
        pass


def get_process_memory_mb(pid):
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
            return round(counters.WorkingSetSize / (1024 * 1024), 1)
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
        elif FLAG_CLUSTER.match(arg):
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
    mem_str = f" · {mem} MB working set" if mem is not None else ""
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
    existing = awake_state.read_owned()
    if existing:
        mem = get_process_memory_mb(existing["pid"])
        mem_str = f" ({mem} MB working set)" if mem else ""
        print(f"Keep Awake is already running in background (PID: {existing['pid']}{mem_str}).")
        return 0
    # Prefer pythonw.exe so no console window is attached.
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    py_exe = str(pythonw) if pythonw.exists() else sys.executable
    cmd = [py_exe, str(Path(__file__).resolve()), *options["forward"], "--daemon"]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW, close_fds=True)
    time.sleep(0.1)
    awake_state.save(awake_state.record(
        proc.pid, flags=flags_of(options), display=options["display"], duration=options["duration"],
        mode=session_mode(options), watch_pid=options["pid"], background=True))
    disp_msg = "System & display sleep prevented." if options["display"] else "Idle system sleep prevented (display can sleep)."
    print(f"✓ Keep Awake started in background (PID: {proc.pid}).")
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

    if options["daemon"]:
        awake_state.save(awake_state.record(
            os.getpid(), flags=flags_of(options), display=options["display"], duration=options["duration"],
            mode=session_mode(options), watch_pid=options["pid"], background=True))

    job = kill_on_close_job() if options["command"] else None  # noqa: F841 - kept open for our lifetime
    duration, pid = options["duration"], options["pid"]
    set_awake_state(options["display"])
    trim_memory()

    try:
        if options["command"]:
            try:
                proc = subprocess.Popen(options["command"])
            except OSError as exc:
                print(f"windows_caffeinate: cannot run {options['command'][0]}: {exc}", file=sys.stderr)
                return 127
            return proc.wait()
        if pid:
            start_time = time.monotonic()
            loop_count = 0
            while True:
                if duration is not None and (time.monotonic() - start_time) >= duration:
                    break
                if not is_pid_alive(pid):
                    break
                time.sleep(0.5)
                loop_count += 1
                if loop_count % 60 == 0:
                    trim_memory()
        elif duration is not None:
            start_time = time.monotonic()
            loop_count = 0
            while (time.monotonic() - start_time) < duration:
                time.sleep(max(0.0, min(1.0, duration - (time.monotonic() - start_time))))
                loop_count += 1
                if loop_count % 60 == 0:
                    trim_memory()
        else:
            loop_count = 0
            while True:
                time.sleep(1)
                loop_count += 1
                if loop_count % 30 == 0:
                    set_awake_state(options["display"])
                    trim_memory()
        return 0
    finally:
        clear_awake_state()
        if options["daemon"]:
            awake_state.remove(only_if_pid=os.getpid())


if __name__ == "__main__":
    sys.exit(main())
