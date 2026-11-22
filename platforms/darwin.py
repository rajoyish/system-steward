"""macOS (Darwin) mechanics: process identity and Mach / sysctl readings."""
import ctypes
import functools
import os
import signal
import sys
from typing import Any


def process_identity(pid: int) -> dict[str, Any] | None:
    """{"alive", "create_time", "exe"} for a live PID, or None when it is not running."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return {"alive": True, "create_time": None, "exe": None}
    import subprocess
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "comm="],
                                capture_output=True, text=True, timeout=3, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return {"alive": True, "create_time": None, "exe": None}
    parts = result.stdout.split(None, 5)
    if result.returncode or len(parts) < 6:
        return None
    return {"alive": True, "create_time": " ".join(parts[:5]), "exe": parts[5].strip()}


def terminate_pid(pid: int) -> bool:
    try:
        os.kill(int(pid), signal.SIGTERM)
        return True
    except OSError:
        return False


@functools.cache
def libsystem() -> ctypes.CDLL:
    """Load and prototype libSystem once per process instead of on every 5 s sample."""
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    lib.mach_host_self.restype = ctypes.c_uint
    lib.host_statistics.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    lib.host_statistics.restype = ctypes.c_int
    lib.mach_port_deallocate.argtypes = [ctypes.c_uint, ctypes.c_uint]
    lib.mach_port_deallocate.restype = ctypes.c_int
    lib.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    lib.sysctlbyname.restype = ctypes.c_int
    return lib


class Host:
    """Darwin Mach host_statistics ABI from the macOS SDK's mach/host_info.h."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise OSError("Live system metrics require macOS")
        self.lib = libsystem()

    def sysctl(self, name: str) -> bytes:
        size = ctypes.c_size_t()
        if self.lib.sysctlbyname(name.encode(), None, ctypes.byref(size), None, 0):
            raise OSError(ctypes.get_errno(), f"Cannot read {name}")
        result = ctypes.create_string_buffer(size.value)
        if self.lib.sysctlbyname(name.encode(), result, ctypes.byref(size), None, 0):
            raise OSError(ctypes.get_errno(), f"Cannot read {name}")
        return result.raw[:size.value]

    def integer(self, name: str) -> int:
        return int.from_bytes(self.sysctl(name), sys.byteorder)

    def text(self, name: str) -> str:
        return self.sysctl(name).rstrip(b"\0").decode(errors="replace")

    def cpu_ticks(self) -> tuple[int, ...]:
        # CPU_STATE_USER, SYSTEM, IDLE, NICE; natural_t is uint32 on Darwin.
        ticks = (ctypes.c_uint * 4)()
        count = ctypes.c_uint(4)
        host = self.lib.mach_host_self()
        try:
            result = self.lib.host_statistics(host, 3, ticks, ctypes.byref(count))
            if result or count.value < 4:
                raise OSError(f"CPU statistics unavailable (Mach error {result})")
            return tuple(ticks)
        finally:
            task = ctypes.c_uint.in_dll(self.lib, "mach_task_self_").value
            self.lib.mach_port_deallocate(task, host)
