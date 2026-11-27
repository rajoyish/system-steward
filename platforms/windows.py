"""Every Win32 call System Steward makes, via ctypes, in one place.

Prototypes are declared once (``api()``), with ``use_last_error`` so failures report real codes.
Only ctypes/functools/os/time are imported at module load: the background Keep Awake helper
imports this module and should stay a minimal interpreter.
"""
from collections.abc import Sequence
import ctypes
from ctypes import wintypes
import functools
import os
from pathlib import Path
import platform as _platform
import shutil
import time
from typing import Any

# {(pid, creation_time): (exe_name, cpu_time_100ns, working_set_bytes)}
UsageSnapshot = dict[tuple[int, int], tuple[str, int, int]]

# --------------------------------------------------------------------------- constants

PROCESS_TERMINATE = 0x0001
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259
ERROR_ACCESS_DENIED = 5
ERROR_NO_MORE_FILES = 18
WAIT_TIMEOUT = 0x00000102
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE = 2, 3, 4
IF_TYPE_SOFTWARE_LOOPBACK = 24
IF_OPER_STATUS_UP = 1
FO_DELETE = 3
FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI, FOF_WANTNUKEWARNING = 0x4, 0x10, 0x40, 0x400, 0x4000

# --------------------------------------------------------------------------- structures


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


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
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


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("sullAvailExtendedVirtual", ctypes.c_uint64),
    ]


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class MIB_IF_ROW2(ctypes.Structure):
    """netioapi.h MIB_IF_ROW2; 1352 bytes on x64 (checked by the tests)."""
    _fields_ = [
        ("InterfaceLuid", ctypes.c_uint64),
        ("InterfaceIndex", wintypes.ULONG),
        ("InterfaceGuid", GUID),
        ("Alias", wintypes.WCHAR * 257),
        ("Description", wintypes.WCHAR * 257),
        ("PhysicalAddressLength", wintypes.ULONG),
        ("PhysicalAddress", ctypes.c_ubyte * 32),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * 32),
        ("Mtu", wintypes.ULONG),
        ("Type", wintypes.ULONG),
        ("TunnelType", ctypes.c_int),
        ("MediaType", ctypes.c_int),
        ("PhysicalMediumType", ctypes.c_int),
        ("AccessType", ctypes.c_int),
        ("DirectionType", ctypes.c_int),
        ("InterfaceAndOperStatusFlags", ctypes.c_ubyte),  # bit 0 HardwareInterface, bit 1 FilterInterface
        ("OperStatus", ctypes.c_int),
        ("AdminStatus", ctypes.c_int),
        ("MediaConnectState", ctypes.c_int),
        ("NetworkGuid", GUID),
        ("ConnectionType", ctypes.c_int),
        ("TransmitLinkSpeed", ctypes.c_uint64),
        ("ReceiveLinkSpeed", ctypes.c_uint64),
        ("InOctets", ctypes.c_uint64),
        ("InUcastPkts", ctypes.c_uint64),
        ("InNUcastPkts", ctypes.c_uint64),
        ("InDiscards", ctypes.c_uint64),
        ("InErrors", ctypes.c_uint64),
        ("InUnknownProtos", ctypes.c_uint64),
        ("InUcastOctets", ctypes.c_uint64),
        ("InMulticastOctets", ctypes.c_uint64),
        ("InBroadcastOctets", ctypes.c_uint64),
        ("OutOctets", ctypes.c_uint64),
        ("OutUcastPkts", ctypes.c_uint64),
        ("OutNUcastPkts", ctypes.c_uint64),
        ("OutDiscards", ctypes.c_uint64),
        ("OutErrors", ctypes.c_uint64),
        ("OutUcastOctets", ctypes.c_uint64),
        ("OutMulticastOctets", ctypes.c_uint64),
        ("OutBroadcastOctets", ctypes.c_uint64),
        ("OutQLen", ctypes.c_uint64),
    ]


class MIB_IF_TABLE2(ctypes.Structure):
    _fields_ = [("NumEntries", wintypes.ULONG), ("Table", MIB_IF_ROW2 * 1)]


class SHFILEOPSTRUCTW(ctypes.Structure):
    if ctypes.sizeof(ctypes.c_void_p) == 4:
        _pack_ = 1  # shellapi.h packs this structure on 32-bit Windows.
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR), ("fFlags", wintypes.WORD), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", wintypes.LPVOID), ("lpszProgressTitle", wintypes.LPCWSTR)]


# --------------------------------------------------------------------------- prototypes


class _Api:
    def __init__(self) -> None:
        self.kernel32 = k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.psapi = p = ctypes.WinDLL("psapi", use_last_error=True)
        self.iphlpapi = n = ctypes.WinDLL("iphlpapi", use_last_error=True)
        self.shell32 = s = ctypes.WinDLL("shell32", use_last_error=True)
        H, D, B = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
        FT = ctypes.POINTER(wintypes.FILETIME)
        for name, args, result in (
            ("OpenProcess", (D, B, D), H),
            ("CloseHandle", (H,), B),
            ("GetCurrentProcess", (), H),
            ("GetExitCodeProcess", (H, ctypes.POINTER(D)), B),
            ("GetProcessTimes", (H, FT, FT, FT, FT), B),
            ("QueryFullProcessImageNameW", (H, D, wintypes.LPWSTR, ctypes.POINTER(D)), B),
            ("TerminateProcess", (H, wintypes.UINT), B),
            ("WaitForSingleObject", (H, D), D),
            ("CreateToolhelp32Snapshot", (D, D), H),
            ("Process32FirstW", (H, ctypes.POINTER(PROCESSENTRY32W)), B),
            ("Process32NextW", (H, ctypes.POINTER(PROCESSENTRY32W)), B),
            ("CreateJobObjectW", (wintypes.LPVOID, wintypes.LPCWSTR), H),
            ("SetInformationJobObject", (H, ctypes.c_int, wintypes.LPVOID, D), B),
            ("AssignProcessToJobObject", (H, H), B),
            ("SetThreadExecutionState", (D,), D),
            ("GetSystemTimes", (FT, FT, FT), B),
            ("GlobalMemoryStatusEx", (ctypes.POINTER(MEMORYSTATUSEX),), B),
            ("GetSystemPowerStatus", (ctypes.POINTER(SYSTEM_POWER_STATUS),), B),
            ("GetTickCount64", (), ctypes.c_uint64),  # default c_int would wrap after ~24.8 days
            ("GetLogicalDrives", (), D),
            ("GetDriveTypeW", (wintypes.LPCWSTR,), wintypes.UINT),
            ("GetDiskFreeSpaceW", (wintypes.LPCWSTR, ctypes.POINTER(D), ctypes.POINTER(D),
                                   ctypes.POINTER(D), ctypes.POINTER(D)), B),
            ("GetCompressedFileSizeW", (wintypes.LPCWSTR, ctypes.POINTER(D)), D),
        ):
            function = getattr(k, name)
            function.argtypes, function.restype = args, result
        p.GetProcessMemoryInfo.argtypes = (H, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), D)
        p.GetProcessMemoryInfo.restype = B
        n.GetIfTable2.argtypes = (ctypes.POINTER(ctypes.POINTER(MIB_IF_TABLE2)),)
        n.GetIfTable2.restype = wintypes.ULONG
        n.FreeMibTable.argtypes = (wintypes.LPVOID,)
        n.FreeMibTable.restype = None
        s.SHFileOperationW.argtypes = (ctypes.POINTER(SHFILEOPSTRUCTW),)
        s.SHFileOperationW.restype = ctypes.c_int


@functools.cache
def api() -> _Api:
    return _Api()


def _filetime(ft: wintypes.FILETIME) -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


# --------------------------------------------------------------------------- processes

def process_identity(pid: int) -> dict[str, Any] | None:
    """{"alive", "create_time", "exe"} for a live PID, or None when it is not running."""
    k = api().kernel32
    handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        if ctypes.get_last_error() == ERROR_ACCESS_DENIED:
            # Alive, but not inspectable by this user, so it cannot be our session.
            return {"alive": True, "create_time": None, "exe": None}
        return None
    try:
        code = wintypes.DWORD()
        if not k.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        create_time = None
        if k.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            create_time = _filetime(created)
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        exe = buffer.value if k.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else None
        return {"alive": True, "create_time": create_time, "exe": exe}
    finally:
        k.CloseHandle(handle)


def terminate_pid(pid: int) -> bool:
    k = api().kernel32
    handle = k.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not handle:
        return False
    try:
        return bool(k.TerminateProcess(handle, 1))
    finally:
        k.CloseHandle(handle)


def private_memory_mb(pid: object) -> float | None:
    """Private (committed) memory of ``pid`` in MiB: the honest footprint, unlike the trimmable working set."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    a = api()
    handle = a.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return None
    try:
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
        if a.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return round(counters.PrivateUsage / (1024 * 1024), 1)
        return None
    finally:
        a.kernel32.CloseHandle(handle)


def wait_for_process(pid: int, duration: float | None = None) -> None:
    """Block until ``pid`` exits or ``duration`` seconds pass, without polling process state.

    Waits on the process handle in 1 s slices only so Ctrl+C stays responsive in a console.
    """
    k = api().kernel32
    handle = k.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not handle:
        return
    deadline = None if duration is None else time.monotonic() + duration
    try:
        while True:
            slice_ms = 1000 if deadline is None else max(0, min(1000, int((deadline - time.monotonic()) * 1000)))
            if k.WaitForSingleObject(handle, slice_ms) != WAIT_TIMEOUT:
                return  # process exited (or the wait failed)
            if deadline is not None and time.monotonic() >= deadline:
                return
    finally:
        k.CloseHandle(handle)


def kill_on_close_job() -> tuple[int | None, int]:
    """Put the current process in a job that kills every member when this process dies.

    Children spawned afterwards inherit the job, so terminating the helper also ends a wrapped
    command and its descendants. Returns the handle (keep it open), or None with the error code.
    """
    k = api().kernel32
    job = k.CreateJobObjectW(None, None)
    if not job:
        return None, ctypes.get_last_error()
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if (k.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info))
            and k.AssignProcessToJobObject(job, k.GetCurrentProcess())):
        return job, 0
    error = ctypes.get_last_error()
    k.CloseHandle(job)
    return None, error


def set_execution_state(display: bool = False, clear: bool = False) -> None:
    """Request (or release) idle/display sleep prevention for the calling thread.

    Raises OSError if Windows rejects the request (the API returns 0), so a session never
    reports itself active while nothing is actually holding the machine awake.
    """
    flags = ES_CONTINUOUS if clear else ES_CONTINUOUS | ES_SYSTEM_REQUIRED | (ES_DISPLAY_REQUIRED if display else 0)
    if not api().kernel32.SetThreadExecutionState(flags) and not clear:
        raise OSError(ctypes.get_last_error(), "SetThreadExecutionState rejected the sleep request")


# SYSTEM_POWER_CAPABILITIES (winnt.h) is 76 bytes; AoAc ("always on, always connected",
# i.e. Modern Standby / S0 Low Power Idle) is the BOOLEAN at offset 20.
MODERN_STANDBY_WARNING = ("this PC uses Modern Standby, so it still enters standby when the screen turns off. "
                          "Also prevent display sleep (-d) to keep it fully awake.")
SYSTEM_POWER_CAPABILITIES_LEVEL = 4
SYSTEM_POWER_CAPABILITIES_SIZE = 76
AOAC_OFFSET = 20


@functools.cache
def modern_standby() -> bool:
    """True when this PC sleeps with Modern Standby (S0 Low Power Idle) instead of S3.

    On these systems the display idle timeout also enters standby, which an idle-only
    (ES_SYSTEM_REQUIRED) request does not prevent; keeping the display on does.
    """
    try:
        powrprof = ctypes.WinDLL("powrprof")
        buffer = (ctypes.c_ubyte * SYSTEM_POWER_CAPABILITIES_SIZE)()
        status = powrprof.CallNtPowerInformation(SYSTEM_POWER_CAPABILITIES_LEVEL, None, 0,
                                                 buffer, SYSTEM_POWER_CAPABILITIES_SIZE)
    except (OSError, AttributeError):
        return False
    return status == 0 and bool(buffer[AOAC_OFFSET])


def spawn_detached(cmd: Sequence[str], cwd: str | os.PathLike[str]) -> int | None:
    """Start ``cmd`` through WMI so it is outside the terminal's job object and survives it closing.

    The command line and folder travel in environment variables, never inside the script text,
    so paths containing quotes cannot change the PowerShell that runs. Returns the PID or None.
    """
    import subprocess
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


def processes() -> list[tuple[int, str]]:
    """[(pid, exe_name)] for every running process, from one Toolhelp snapshot."""
    k = api().kernel32
    snapshot = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        result = []
        ok = k.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            result.append((entry.th32ProcessID, entry.szExeFile))
            ok = k.Process32NextW(snapshot, ctypes.byref(entry))
        error = ctypes.get_last_error()
        if error not in (0, ERROR_NO_MORE_FILES):
            raise OSError(error, "Process enumeration failed")
        if not result:
            raise OSError("Process enumeration returned nothing")
        return result
    finally:
        k.CloseHandle(snapshot)


def process_names() -> list[str]:
    """Lower-cased executable names of running processes, e.g. "chrome.exe"."""
    return [name.lower() for _, name in processes()]


def process_usage(pid: int) -> tuple[int, int, int] | None:
    """(creation_time, cpu_time_100ns, working_set_bytes) or None when the process is not inspectable."""
    a = api()
    handle = a.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not a.kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(kernel), ctypes.byref(user)):
            return None
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
        working_set = counters.WorkingSetSize if a.psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb) else 0
        return _filetime(created), _filetime(kernel) + _filetime(user), working_set
    finally:
        a.kernel32.CloseHandle(handle)


def usage_snapshot() -> UsageSnapshot:
    """{(pid, creation_time): (name, cpu_time_100ns, working_set)}; keyed by creation time so a
    reused PID is never compared with the previous owner's CPU time."""
    snapshot = {}
    for pid, name in processes():
        if pid == 0:
            continue  # System Idle Process
        usage = process_usage(pid)
        if usage:
            created, cpu, working_set = usage
            snapshot[(pid, created)] = (name, cpu, working_set)
    return snapshot


def top_processes(before: UsageSnapshot, after: UsageSnapshot, elapsed: float, total_ram: int,
                  cpu_count: int | None, limit: int = 10) -> list[dict[str, Any]]:
    """Top processes by CPU share between two usage snapshots.

    CPU is a percentage of total machine capacity (Task Manager's convention), so it is 0-100.
    """
    capacity = elapsed * 1e7 * max(1, cpu_count or 1)  # 100 ns units across all logical CPUs
    rows = []
    for key, (name, cpu, working_set) in after.items():
        previous = before.get(key)
        if previous is None or capacity <= 0:
            continue
        share = max(0.0, min(100.0, (cpu - previous[1]) / capacity * 100))
        memory = round(working_set / total_ram * 100, 1) if total_ram else 0.0
        rows.append({"pid": key[0], "cpu": round(share, 1), "memory": memory,
                     "name": name[:-4] if name.lower().endswith(".exe") else name})
    rows.sort(key=lambda item: (item["cpu"], item["memory"]), reverse=True)
    return rows[:limit]


# --------------------------------------------------------------------------- network

def interface_counters() -> dict[str, tuple[int, int]]:
    """{alias: (received_bytes, sent_bytes)} for connected, non-loopback, non-filter interfaces."""
    n = api().iphlpapi
    table = ctypes.POINTER(MIB_IF_TABLE2)()
    error = n.GetIfTable2(ctypes.byref(table))
    if error:
        raise OSError(error, "GetIfTable2 failed")
    try:
        count = table.contents.NumEntries
        rows = ctypes.cast(ctypes.addressof(table.contents.Table), ctypes.POINTER(MIB_IF_ROW2 * count)).contents
        counters = {}
        for row in rows:
            is_filter = bool(row.InterfaceAndOperStatusFlags & 0x2)
            if row.Type == IF_TYPE_SOFTWARE_LOOPBACK or is_filter or row.OperStatus != IF_OPER_STATUS_UP:
                continue
            if not (row.InOctets or row.OutOctets):
                continue
            name = row.Alias or row.Description or f"Interface {row.InterfaceIndex}"
            previous = counters.get(name, (0, 0))
            counters[name] = (previous[0] + row.InOctets, previous[1] + row.OutOctets)
        return counters
    finally:
        n.FreeMibTable(table)


# --------------------------------------------------------------------------- storage

def drive_type(root: str) -> int:
    return api().kernel32.GetDriveTypeW(root)


def cluster_size(path: str | os.PathLike[str]) -> int:
    """Allocation unit of the volume holding ``path`` (4 KiB fallback)."""
    sectors, sector_bytes, free, total = (wintypes.DWORD() for _ in range(4))
    try:
        if api().kernel32.GetDiskFreeSpaceW(Path(path).anchor or None, ctypes.byref(sectors), ctypes.byref(sector_bytes),
                                            ctypes.byref(free), ctypes.byref(total)):
            return max(512, sectors.value * sector_bytes.value)
    except (OSError, ValueError):
        pass
    return 4096


def on_disk_size(path: str, fallback: int) -> int:
    """Real on-disk size of a compressed / sparse / compacted file."""
    high = wintypes.DWORD()
    low = api().kernel32.GetCompressedFileSizeW(path, ctypes.byref(high))
    if low == 0xFFFFFFFF and ctypes.get_last_error():
        return fallback
    return (high.value << 32) | low


# SHFileOperationW returns Win32 error codes for most failures; name the common ones.
SHELL_ERRORS = {
    0x5: "access denied",
    0x20: "it is open in another program (close that program, then retry)",
    0x21: "part of it is locked by another program",
    0x2: "it no longer exists",
    0x3: "it no longer exists",
    0x78: "access denied",  # DE_ACCESSDENIEDSRC
    0x7C: "the path is not valid",  # DE_INVALIDFILES
    0x4C7: "the operation was cancelled",
}


def recycle(path: str | os.PathLike[str]) -> None:
    """Send one reviewed path to the Recycle Bin (restorable from Explorer).

    Only local fixed drives are accepted: on network, removable or RAM drives the shell would
    silently delete permanently instead of recycling. FOF_WANTNUKEWARNING is a second guard that
    asks before any permanent deletion the shell decides on anyway.
    """
    path = Path(path)
    if not path.is_absolute() or any(c in str(path) for c in "*?"):
        raise ValueError(f"Refusing an unexpected Recycle Bin path: {path}")
    if drive_type(path.anchor) != DRIVE_FIXED:
        raise ValueError(f"The Recycle Bin is unavailable on {path.anchor}; nothing was moved")
    operation = SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom=str(path) + "\0",  # list is double-NUL terminated
                                fFlags=FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI | FOF_WANTNUKEWARNING)
    result = api().shell32.SHFileOperationW(ctypes.byref(operation))
    if result or operation.fAnyOperationsAborted:
        reason = SHELL_ERRORS.get(result, f"shell error {result:#x}")
        raise OSError(f"Could not move {path} to the Recycle Bin: {reason}")
    if os.path.lexists(path):
        raise OSError(f"{path} is still present after moving it to the Recycle Bin")


def drives() -> list[dict[str, Any]]:
    """Usage of local, removable and network drive letters."""
    result = []
    bitmask = api().kernel32.GetLogicalDrives()
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if bitmask & 1:
            root = f"{letter}:\\"
            if drive_type(root) in (DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE):
                try:
                    usage = shutil.disk_usage(root)
                    result.append({"mount": root, "used": usage.used, "total": usage.total,
                                   "used_percent": (usage.used / usage.total * 100) if usage.total else 0.0})
                except OSError:
                    pass
        bitmask >>= 1
    return result


# --------------------------------------------------------------------------- system metrics

class Host:
    """Windows system resource sampling using Win32 APIs."""

    def __init__(self) -> None:
        self.kernel32 = api().kernel32

    def cpu_ticks(self) -> tuple[int, int, int]:
        idle, kernel, user = (wintypes.FILETIME() for _ in range(3))
        if not self.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            raise OSError(ctypes.get_last_error(), "GetSystemTimes failed")
        return (_filetime(idle), _filetime(kernel), _filetime(user))

    def memory_stats(self) -> dict[str, Any]:
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not self.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            raise OSError(ctypes.get_last_error(), "GlobalMemoryStatusEx failed")
        used = stat.ullTotalPhys - stat.ullAvailPhys
        used_percent = (used / stat.ullTotalPhys * 100) if stat.ullTotalPhys else 0.0
        return {"total": stat.ullTotalPhys, "used": used, "used_percent": used_percent, "compressed": 0,
                "cached": stat.ullAvailPhys, "definition": "Physical RAM in use; excludes available standby cache."}

    def hardware_info(self, total_ram: int) -> dict[str, Any]:
        cpu_name = _platform.processor()
        model = "Windows PC"
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                cpu_name = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS") as k:
                model = winreg.QueryValueEx(k, "SystemProductName")[0].strip()
        except OSError:
            pass
        return {"model": model, "cpu_model": cpu_name,
                "os_version": f"Windows {_platform.version()} ({_platform.release()})", "total_ram": total_ram}

    def uptime_seconds(self) -> int:
        return int(self.kernel32.GetTickCount64() // 1000)

    def power_status(self) -> list[dict[str, Any]]:
        s = SYSTEM_POWER_STATUS()
        if not self.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
            return []
        if s.BatteryFlag & 128 or s.BatteryLifePercent == 255:
            return []
        percent = int(s.BatteryLifePercent)
        if s.BatteryFlag & 8 or (s.ACLineStatus == 1 and percent < 100):
            status = "charging"
        elif s.ACLineStatus == 1:
            status = "AC Power"
        else:
            status = "discharging"
        time_left = ""
        if s.BatteryLifeTime != 0xFFFFFFFF and s.BatteryLifeTime > 0:
            time_left = f"{s.BatteryLifeTime // 3600}h {s.BatteryLifeTime % 3600 // 60:02d}m remaining"
        return [{"percent": percent, "status": status, "time_left": time_left}]
