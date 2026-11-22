"""Native Windows process and network readings via ctypes (no PowerShell / netstat child processes).

Every function here is read-only. Import only on Windows.
"""
import ctypes
from ctypes import wintypes
import functools

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_NO_MORE_FILES = 18
IF_TYPE_SOFTWARE_LOOPBACK = 24
IF_OPER_STATUS_UP = 1


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


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
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


@functools.cache
def _api():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD)
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    iphlpapi.GetIfTable2.argtypes = (ctypes.POINTER(ctypes.POINTER(MIB_IF_TABLE2)),)
    iphlpapi.GetIfTable2.restype = wintypes.ULONG
    iphlpapi.FreeMibTable.argtypes = (wintypes.LPVOID,)
    iphlpapi.FreeMibTable.restype = None
    return kernel32, psapi, iphlpapi


def processes():
    """[(pid, exe_name)] for every running process, from one Toolhelp snapshot."""
    kernel32, _, _ = _api()
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        result = []
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            result.append((entry.th32ProcessID, entry.szExeFile))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        error = ctypes.get_last_error()
        if error not in (0, ERROR_NO_MORE_FILES):
            raise OSError(error, "Process enumeration failed")
        if not result:
            raise OSError("Process enumeration returned nothing")
        return result
    finally:
        kernel32.CloseHandle(snapshot)


def process_usage(pid):
    """(creation_time, cpu_time_100ns, working_set_bytes) or None when the process is not inspectable."""
    kernel32, psapi, _ = _api()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return None
        as_int = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        working_set = counters.WorkingSetSize if psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb) else 0
        return as_int(created), as_int(kernel) + as_int(user), working_set
    finally:
        kernel32.CloseHandle(handle)


def usage_snapshot():
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


def top_processes(before, after, elapsed, total_ram, cpu_count, limit=10):
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


def process_names():
    """Lower-cased executable names of running processes, e.g. "chrome.exe"."""
    return [name.lower() for _, name in processes()]


def interface_counters():
    """{alias: (received_bytes, sent_bytes)} for connected, non-loopback, non-filter interfaces."""
    _, _, iphlpapi = _api()
    table = ctypes.POINTER(MIB_IF_TABLE2)()
    error = iphlpapi.GetIfTable2(ctypes.byref(table))
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
        iphlpapi.FreeMibTable(table)
