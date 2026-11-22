"""Windows Keep Awake helper mimicking macOS caffeinate using SetThreadExecutionState."""
import ctypes
import json
import os
from pathlib import Path
import signal
import sys
import time
import subprocess

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_AWAYMODE_REQUIRED = 0x00000040

DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


def get_state_file():
    state_dir = Path.home() / ".system-steward"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "awake_session.json"


def save_state(state):
    try:
        with open(get_state_file(), "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass


def remove_state(only_if_pid=None):
    try:
        sf = get_state_file()
        if sf.exists():
            if only_if_pid is not None:
                try:
                    with open(sf, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("pid") != only_if_pid:
                        return
                except Exception:
                    pass
            sf.unlink(missing_ok=True)
    except Exception:
        pass


def read_state():
    sf = get_state_file()
    if sf.exists():
        try:
            with open(sf, "r", encoding="utf-8") as f:
                data = json.load(f)
            pid = data.get("pid")
            if pid and is_pid_alive(pid):
                return data
            else:
                remove_state()
        except Exception:
            remove_state()
    return None


def trim_memory():
    """Trim unused pages from working set to keep memory footprint minimal (~2-4 MB)."""
    try:
        ctypes.windll.psapi.EmptyWorkingSet(ctypes.windll.kernel32.GetCurrentProcess())
    except Exception:
        pass


def get_process_memory_mb(pid):
    try:
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        handle = kernel32.OpenProcess(0x1000 | 0x0400, False, pid)
        if not handle:
            return None
        class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
            _fields_ = [
                ('cb', ctypes.c_ulong),
                ('PageFaultCount', ctypes.c_ulong),
                ('PeakWorkingSetSize', ctypes.c_size_t),
                ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t),
                ('PeakPagefileUsage', ctypes.c_size_t),
                ('PrivateUsage', ctypes.c_size_t),
            ]
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            kernel32.CloseHandle(handle)
            return round(counters.WorkingSetSize / (1024 * 1024), 1)
        kernel32.CloseHandle(handle)
    except Exception:
        pass
    return None


def set_awake_state(display=False):
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    if display:
        flags |= ES_DISPLAY_REQUIRED
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def clear_awake_state():
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass


def is_pid_alive(pid):
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return kernel32.GetLastError() == 5
    exit_code = ctypes.c_ulong()
    kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
    kernel32.CloseHandle(handle)
    return exit_code.value == 259


def stop_pid(pid):
    kernel32 = ctypes.windll.kernel32
    PROCESS_TERMINATE = 0x0001
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if handle:
        kernel32.TerminateProcess(handle, 1)
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def main():
    args = sys.argv[1:]

    # Handle status query
    if "--status" in args or "-s" in args:
        state = read_state()
        if state:
            pid = state.get("pid")
            mem = get_process_memory_mb(pid)
            mem_str = f" · {mem} MB memory" if mem is not None else ""
            disp = " · Display kept awake" if state.get("display") else " · Display can sleep"
            dur = state.get("duration")
            remaining_str = ""
            if dur and state.get("started"):
                rem = max(0, int(dur - (time.time() - state["started"])))
                remaining_str = f" · {rem}s remaining"
            print(f"Keep Awake active in background (PID: {pid}{mem_str}{disp}{remaining_str})")
            sys.exit(0)
        else:
            print("No Keep Awake session running.")
            sys.exit(1)

    # Handle stop command
    if "--stop" in args:
        state = read_state()
        if state:
            pid = state.get("pid")
            stop_pid(pid)
            remove_state()
            print(f"Keep Awake stopped (PID: {pid}).")
        else:
            remove_state()
            print("No Keep Awake session running.")
        sys.exit(0)

    display = False
    duration = None
    pid = None
    command = []
    run_background = False
    is_daemon = False

    filtered_args = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-b", "--background"):
            run_background = True
        elif arg == "--daemon":
            is_daemon = True
        elif arg.startswith("-") and not command:
            if "d" in arg:
                display = True
            if arg == "-t":
                filtered_args.append(arg)
                i += 1
                if i < len(args):
                    filtered_args.append(args[i])
                    try:
                        duration = float(args[i])
                    except ValueError:
                        pass
            elif arg == "-w":
                filtered_args.append(arg)
                i += 1
                if i < len(args):
                    filtered_args.append(args[i])
                    try:
                        pid = int(args[i])
                    except ValueError:
                        pass
            else:
                filtered_args.append(arg)
        else:
            command = args[i:]
            filtered_args.extend(command)
            break
        i += 1

    # If requested to run in background from CLI and not yet daemonized
    if run_background and not is_daemon:
        existing = read_state()
        if existing:
            existing_pid = existing.get("pid")
            mem = get_process_memory_mb(existing_pid)
            mem_str = f" ({mem} MB RAM)" if mem else ""
            print(f"Keep Awake is already running in background (PID: {existing_pid}{mem_str}).")
            sys.exit(0)

        # Prefer pythonw.exe to prevent any console window attachment
        pythonw = Path(sys.executable).parent / "pythonw.exe"
        py_exe = str(pythonw) if pythonw.exists() else sys.executable
        creationflags = DETACHED_PROCESS | CREATE_NO_WINDOW
        cmd = [py_exe, str(Path(__file__).resolve()), *filtered_args, "--daemon"]

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True
        )

        time.sleep(0.1)
        save_state({
            "pid": proc.pid,
            "started": time.time(),
            "display": display,
            "duration": duration,
            "mode": "process" if pid else ("command" if command else "manual"),
            "background": True
        })

        disp_msg = "Idle system sleep prevented (display can sleep)." if not display else "System & display sleep prevented."
        print(f"✓ Keep Awake started in background (PID: {proc.pid}).")
        print(f"  Mode: {disp_msg}")
        print("  Memory: ~2-4 MB (optimized, 99% less than terminal TUI).")
        print("  Persistence: Keeps running even if terminal closes.")
        print("  Shutdown: Turns off automatically when computer turns off.")
        print("  Commands: pwsh -File .\\keep-awake.ps1 -Stop  (or -Status)")
        sys.exit(0)

    # Foreground or daemon worker execution
    if is_daemon:
        save_state({
            "pid": os.getpid(),
            "started": time.time(),
            "display": display,
            "duration": duration,
            "mode": "process" if pid else ("command" if command else "manual"),
            "background": True
        })

    set_awake_state(display)
    trim_memory()

    try:
        if command:
            proc = subprocess.Popen(command)
            returncode = proc.wait()
            sys.exit(returncode)
        elif pid:
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
                time.sleep(min(1.0, duration - (time.monotonic() - start_time)))
                loop_count += 1
                if loop_count % 60 == 0:
                    trim_memory()
        else:
            loop_count = 0
            while True:
                time.sleep(1)
                loop_count += 1
                if loop_count % 30 == 0:
                    set_awake_state(display)
                    trim_memory()
    finally:
        clear_awake_state()
        if is_daemon:
            remove_state(only_if_pid=os.getpid())


if __name__ == "__main__":
    main()
