"""Windows Keep Awake helper mimicking macOS caffeinate using SetThreadExecutionState."""
import ctypes
import os
import signal
import sys
import time
import subprocess

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_AWAYMODE_REQUIRED = 0x00000040


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


def main():
    args = sys.argv[1:]
    display = False
    duration = None
    pid = None
    command = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-") and not command:
            if "d" in arg:
                display = True
            if arg == "-t":
                i += 1
                if i < len(args):
                    try:
                        duration = float(args[i])
                    except ValueError:
                        pass
            elif arg == "-w":
                i += 1
                if i < len(args):
                    try:
                        pid = int(args[i])
                    except ValueError:
                        pass
        else:
            command = args[i:]
            break
        i += 1

    set_awake_state(display)
    try:
        if command:
            proc = subprocess.Popen(command)
            returncode = proc.wait()
            sys.exit(returncode)
        elif pid:
            start_time = time.monotonic()
            while True:
                if duration is not None and (time.monotonic() - start_time) >= duration:
                    break
                if not is_pid_alive(pid):
                    break
                time.sleep(0.5)
        elif duration is not None:
            time.sleep(duration)
        else:
            while True:
                time.sleep(1)
    finally:
        clear_awake_state()


if __name__ == "__main__":
    main()
