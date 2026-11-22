"""Read-only macOS and Windows resource sampling using native system APIs and bundled tools.

The OS calls themselves live in platforms.windows / platforms.darwin.
"""
import datetime
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from platforms.darwin import Host as MacHost


def WindowsHost():
    """Windows metrics host (platforms.windows.Host); OSError elsewhere, like MacHost off macOS."""
    if sys.platform != "win32":
        raise OSError("Live system metrics require Windows")
    from platforms.windows import Host
    return Host()


def windows_drives():
    from platforms.windows import drives
    return drives()


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
    def __init__(self, platform=None):
        # "win32" or "darwin"; injectable so either backend can be exercised on any host.
        self.platform = platform or sys.platform
        self.previous_network = None
        self.previous_network_time = None
        self.previous_processes = None  # (monotonic time, windows.usage_snapshot())
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

        if self.platform == "win32":
            host = attempt("System APIs", WindowsHost)
            if host:
                from platforms import windows
                if self.previous_processes is None:
                    # First sample: measure process CPU across the same short window as system CPU.
                    snapshot = attempt("Processes", windows.usage_snapshot)
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
                        counters = windows.interface_counters()
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
                    now, snapshot = time.monotonic(), windows.usage_snapshot()
                    previous = self.previous_processes
                    self.previous_processes = (now, snapshot)
                    if previous is None:
                        return []
                    return windows.top_processes(previous[1], snapshot, now - previous[0], total_ram, os.cpu_count())
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
