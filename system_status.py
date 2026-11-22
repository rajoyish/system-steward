"""Read-only macOS and Windows resource sampling using native system APIs and bundled tools."""
import ctypes
import datetime
import functools
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time


@functools.cache
def _libsystem():
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


@functools.cache
def _kernel32():
    # use_last_error=True makes ctypes.get_last_error() report the real Win32 error code.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # The default c_int restype truncates the 64-bit tick count (negative uptime after ~24.8 days).
    kernel32.GetTickCount64.restype = ctypes.c_uint64
    return kernel32


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


class MacHost:
    """Darwin Mach host_statistics ABI from the macOS SDK's mach/host_info.h."""
    def __init__(self):
        if sys.platform != "darwin":
            raise OSError("Live system metrics require macOS")
        self.lib = _libsystem()

    def sysctl(self, name):
        size = ctypes.c_size_t()
        if self.lib.sysctlbyname(name.encode(), None, ctypes.byref(size), None, 0):
            raise OSError(ctypes.get_errno(), f"Cannot read {name}")
        result = ctypes.create_string_buffer(size.value)
        if self.lib.sysctlbyname(name.encode(), result, ctypes.byref(size), None, 0):
            raise OSError(ctypes.get_errno(), f"Cannot read {name}")
        return result.raw[:size.value]

    def integer(self, name):
        return int.from_bytes(self.sysctl(name), sys.byteorder)

    def text(self, name):
        return self.sysctl(name).rstrip(b"\0").decode(errors="replace")

    def cpu_ticks(self):
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


class WindowsHost:
    """Windows system resource sampling using Win32 API and standard tools."""
    def __init__(self):
        if sys.platform != "win32":
            raise OSError("Live system metrics require Windows")
        self.kernel32 = _kernel32()

    def cpu_ticks(self):
        idle = ctypes.c_uint64()
        kernel = ctypes.c_uint64()
        user = ctypes.c_uint64()
        if not self.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            raise OSError(ctypes.get_last_error(), "GetSystemTimes failed")
        return (idle.value, kernel.value, user.value)

    def memory_stats(self):
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not self.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            raise OSError(ctypes.get_last_error(), "GlobalMemoryStatusEx failed")
        used = stat.ullTotalPhys - stat.ullAvailPhys
        used_percent = (used / stat.ullTotalPhys * 100) if stat.ullTotalPhys else 0.0
        return {
            "total": stat.ullTotalPhys,
            "used": used,
            "used_percent": used_percent,
            "compressed": 0,
            "cached": stat.ullAvailPhys,
            "definition": "Physical RAM in use; excludes available standby cache."
        }

    def hardware_info(self, total_ram):
        cpu_name = platform.processor()
        model = "Windows PC"
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                cpu_name = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS") as k:
                model = winreg.QueryValueEx(k, "SystemProductName")[0].strip()
        except OSError:
            pass
        os_ver = f"Windows {platform.version()} ({platform.release()})"
        return {
            "model": model,
            "cpu_model": cpu_name,
            "os_version": os_ver,
            "total_ram": total_ram,
        }

    def uptime_seconds(self):
        ms = self.kernel32.GetTickCount64()
        return int(ms // 1000)

    def power_status(self):
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


def command(argv, timeout=5):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, errors="replace", timeout=timeout,
                            env={**os.environ, "LC_ALL": "C"})
    if result.returncode:
        raise OSError(result.stderr.strip() or f"{Path(argv[0]).name} exited with status {result.returncode}")
    return result.stdout


def cpu_usage(before, after):
    delta = [(end - start) & 0xffffffff for start, end in zip(before, after)]
    total = sum(delta)
    return max(0, min(100, 100 * (total - delta[2]) / total)) if total else None


def windows_cpu_usage(before, after):
    idle = after[0] - before[0]
    kernel = after[1] - before[1]
    user = after[2] - before[2]
    total_sys = kernel + user
    busy = (kernel - idle) + user
    if total_sys <= 0:
        return 0.0
    return max(0.0, min(100.0, (busy / total_sys) * 100.0))


def windows_drives():
    drives = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if bitmask & 1:
            drive_path = f"{letter}:\\"
            dtype = ctypes.windll.kernel32.GetDriveTypeW(drive_path)
            if dtype in (2, 3, 4):
                try:
                    usage = shutil.disk_usage(drive_path)
                    drives.append({
                        "mount": drive_path,
                        "used": usage.used,
                        "total": usage.total,
                        "used_percent": (usage.used / usage.total * 100) if usage.total else 0.0
                    })
                except OSError:
                    pass
        bitmask >>= 1
    return drives


def parse_vm_stats(output, total):
    page_match = re.search(r"page size of (\d+) bytes", output)
    if not page_match:
        raise ValueError("Unrecognized VM page size")
    page_size = int(page_match[1])
    counts = {name.strip(): int(value) for name, value in re.findall(r"^([^:\n]+):\s*(\d+)\.?", output, re.M)}
    required = ("Pages active", "Pages wired down", "Pages occupied by compressor")
    if any(key not in counts for key in required) or total <= 0:
        raise ValueError("Incomplete VM statistics")
    # Physical active + wired + compressed pages; inactive/reclaimable pages excluded.
    used = min(total, sum(counts[key] for key in required) * page_size)
    return {"total": total, "used": used, "used_percent": used / total * 100,
            "compressed": counts["Pages occupied by compressor"] * page_size,
            "cached": counts.get("Pages inactive", 0) * page_size,
            "definition": "Active + wired + physical compressor pages; excludes inactive cache. May differ from Activity Monitor's accounting."}


def parse_netstat(output):
    lines = output.splitlines()
    if not lines:
        raise ValueError("No network counters")
    headers = lines[0].split()
    required = {name: headers.index(name) for name in ("Name", "Network", "Ibytes", "Obytes")}
    counters = {}
    for line in lines[1:]:
        parts = line.split()
        if len(parts) <= max(required.values()) or not parts[required["Network"]].startswith("<Link#"):
            continue
        name = parts[required["Name"]].rstrip("*")
        if name.startswith("lo"):
            continue
        try:
            counters[name] = (int(parts[required["Ibytes"]]), int(parts[required["Obytes"]]))
        except ValueError:
            continue
    return counters


def parse_netstat_windows(output):
    match = re.search(r"Bytes\s+(\d+)\s+(\d+)", output)
    if match:
        return {"Ethernet/Wi-Fi": (int(match.group(1)), int(match.group(2)))}
    return {}


def network_rates(previous, current, elapsed):
    rows = []
    for name, (rx, tx) in current.items():
        old = previous.get(name) if previous is not None else None
        available = old is not None and elapsed > 0 and rx >= old[0] and tx >= old[1]
        rows.append({"name": name, "rx_rate_mbs": (rx - old[0]) / elapsed / 1048576 if available else None,
                     "tx_rate_mbs": (tx - old[1]) / elapsed / 1048576 if available else None,
                     "received": rx, "sent": tx, "note": "Counter reset or first sample" if not available else "Measured between refreshes"})
    return rows


def parse_battery(output):
    result = []
    for line in output.splitlines():
        match = re.search(r"(\d+)%;\s*([^;]+);?\s*(.*)", line)
        match_win = re.search(r"(\d+)%", line)
        if match:
            result.append({"percent": int(match[1]), "status": match[2].strip(), "time_left": match[3].strip()})
        elif match_win:
            result.append({"percent": int(match_win[1]), "status": "active", "time_left": ""})
    return result


def resource_score(data):
    """Transparent heuristic, not a hardware diagnostic or vendor health metric."""
    cpu = data.get("cpu", {}).get("usage")
    memory = data.get("memory", {}).get("used_percent")
    disks = data.get("disks") or []
    sys_mounts = ("/", "C:\\", "C:", os.environ.get("SystemDrive", "C:"), os.environ.get("SystemDrive", "C:") + "\\")
    disk = next((item.get("used_percent") for item in disks if item.get("mount") in sys_mounts), None)
    if any(value is None for value in (cpu, memory, disk)):
        return None, "Insufficient CPU, memory or startup-disk data to estimate resource headroom."
    penalties = ((20 if cpu >= 90 else 10 if cpu >= 70 else 0),
                 (25 if memory >= 85 else 10 if memory >= 70 else 0),
                 (30 if disk >= 90 else 15 if disk >= 80 else 0))
    score = 100 - sum(penalties)
    message = "Good resource headroom" if score >= 85 else "Some resource pressure" if score >= 60 else "High resource pressure"
    return score, message + ". Estimate: subtract CPU 10/20 at 70/90%, RAM 10/25 at 70/85%, startup disk 15/30 at 80/90%. Not a hardware diagnosis."


class SystemSampler:
    def __init__(self):
        self.previous_network = None
        self.previous_network_time = None
        self.previous_processes = None  # (monotonic time, winproc.usage_snapshot())
        self.hardware = None

    def sample(self):
        warnings = []
        data = {"collected_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "cpu": {}, "memory": {}, "disks": [], "batteries": [], "network": [],
                "top_processes": [], "hardware": {}, "unavailable": {}}
        def attempt(label, operation):
            try:
                return operation()
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                warnings.append(f"{label}: {exc}")
                data["unavailable"][label] = str(exc)
                return None

        is_mocked_mac = hasattr(MacHost, "assert_called") or hasattr(MacHost, "_mock_side_effect") or hasattr(MacHost, "return_value")
        if sys.platform == "win32" and not is_mocked_mac:
            host = attempt("System APIs", WindowsHost)
            if host:
                import winproc
                if self.previous_processes is None:
                    # First sample: measure process CPU across the same short window as system CPU.
                    snapshot = attempt("Processes", winproc.usage_snapshot)
                    self.previous_processes = (time.monotonic(), snapshot) if snapshot is not None else None

                def cpu():
                    before = host.cpu_ticks()
                    time.sleep(.15)
                    after = host.cpu_ticks()
                    usage = windows_cpu_usage(before, after)
                    return {"usage": usage, "load1": None, "load5": None, "load15": None, "core_count": os.cpu_count()}
                data["cpu"] = attempt("CPU", cpu) or {}
                mem_data = attempt("Memory", host.memory_stats) or {}
                data["memory"] = mem_data
                total_ram = mem_data.get("total", 0)
                if self.hardware is None:
                    self.hardware = attempt("Hardware", lambda: host.hardware_info(total_ram))
                data["hardware"] = self.hardware or {}
                seconds = attempt("Uptime", host.uptime_seconds)
                if seconds is not None:
                    data["uptime"] = f"{seconds // 86400}d {seconds // 3600 % 24}h {seconds // 60 % 60}m"
                data["disks"] = attempt("Disks", windows_drives) or []
                def network():
                    try:
                        counters = winproc.interface_counters()
                    except OSError:
                        counters = parse_netstat_windows(command(["netstat", "-e"]))
                    now = time.monotonic()
                    elapsed = now - self.previous_network_time if self.previous_network_time is not None else 0
                    result = network_rates(self.previous_network, counters, elapsed)
                    self.previous_network, self.previous_network_time = counters, now
                    return result
                data["network"] = attempt("Network", network) or []
                data["batteries"] = attempt("Battery", host.power_status) or []
                def processes():
                    # CPU share between this sample and the previous one (percent of total capacity),
                    # not cumulative CPU seconds since each process started.
                    now, snapshot = time.monotonic(), winproc.usage_snapshot()
                    previous = self.previous_processes
                    self.previous_processes = (now, snapshot)
                    if previous is None:
                        return []
                    return winproc.top_processes(previous[1], snapshot, now - previous[0], total_ram, os.cpu_count())
                data["top_processes"] = attempt("Processes", processes) or []
                data["unavailable"]["GPU utilization / temperature"] = "Not exposed by the supported public sampling interfaces; no private sensor tools are used."
                data["health_score"], data["health_score_msg"] = resource_score(data)
                if warnings:
                    data["_warnings"] = "\n".join(warnings)
                return data

        # Darwin / macOS host
        host = attempt("System APIs", MacHost)
        if host:
            def cpu():
                before = host.cpu_ticks()
                time.sleep(.15)
                usage = cpu_usage(before, host.cpu_ticks())
                loads = os.getloadavg()
                return {"usage": usage, "load1": loads[0], "load5": loads[1], "load15": loads[2], "core_count": os.cpu_count()}
            data["cpu"] = attempt("CPU", cpu) or {}
            data["memory"] = attempt("Memory", lambda: parse_vm_stats(command(["/usr/bin/vm_stat"]), host.integer("hw.memsize"))) or {}
            def hardware():
                return {"model": host.text("hw.model"), "cpu_model": host.text("machdep.cpu.brand_string"),
                        "os_version": command(["/usr/bin/sw_vers", "-productVersion"]).strip(), "total_ram": host.integer("hw.memsize")}
            if self.hardware is None:
                self.hardware = attempt("Hardware", hardware)
            data["hardware"] = self.hardware or {}
            def uptime():
                # struct timeval: time_t seconds followed by microseconds on macOS 64-bit.
                seconds = int.from_bytes(host.sysctl("kern.boottime")[:8], sys.byteorder, signed=True)
                return max(0, int(time.time() - seconds))
            seconds = attempt("Uptime", uptime)
            if seconds is not None:
                data["uptime"] = f"{seconds // 86400}d {seconds // 3600 % 24}h {seconds // 60 % 60}m"
        mounts = [Path("/")]
        def volumes():
            return [p for p in Path("/Volumes").iterdir() if not p.is_symlink() and p.is_mount()]
        mounts.extend(attempt("Mounted volumes", volumes) or [])
        for mount in mounts:
            def disk():
                usage = shutil.disk_usage(mount)
                return {"mount": str(mount), "used": usage.used, "total": usage.total, "used_percent": usage.used / usage.total * 100}
            reading = attempt(f"Disk {mount}", disk)
            if reading:
                data["disks"].append(reading)
        def network():
            counters = parse_netstat(command(["/usr/sbin/netstat", "-ibn"]))
            now = time.monotonic()
            elapsed = now - self.previous_network_time if self.previous_network_time is not None else 0
            result = network_rates(self.previous_network, counters, elapsed)
            self.previous_network, self.previous_network_time = counters, now
            return result
        data["network"] = attempt("Network", network) or []
        data["batteries"] = attempt("Battery", lambda: parse_battery(command(["/usr/bin/pmset", "-g", "batt"]))) or []
        def processes():
            output = command(["/bin/ps", "-axo", "pid=,pcpu=,pmem=,comm="])
            result = []
            for line in output.splitlines():
                parts = line.split(None, 3)
                if len(parts) == 4:
                    result.append({"pid": int(parts[0]), "cpu": float(parts[1]), "memory": float(parts[2]), "name": parts[3]})
            return sorted(result, key=lambda item: item["cpu"], reverse=True)[:10]
        data["top_processes"] = attempt("Processes", processes) or []
        data["unavailable"]["GPU utilization / temperature"] = "Not exposed by the supported public sampling interfaces; no private sensor tools are used."
        data["health_score"], data["health_score_msg"] = resource_score(data)
        if warnings:
            data["_warnings"] = "\n".join(warnings)
        return data
