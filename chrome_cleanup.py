"""Explicit Chrome cleanup targets; no shell commands or profile-root deletion."""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading


PROFILE_NAME = re.compile(r"(?:Default|Profile \d+|Guest Profile|System Profile)\Z")
SITE_DIRS = (
    "IndexedDB", "Local Storage", "Session Storage", "Service Worker", "WebStorage",
    "File System", "databases", "Application Cache", "Shared Dictionary", "blob_storage",
    "Platform Notifications", "Media Licenses",
)
CACHE_DIRS = (
    "Cache", "Code Cache", "GPUCache", "Media Cache", "DawnCache", "DawnGraphiteCache",
    "DawnWebGPUCache", "ShaderCache", "GrShaderCache", "GraphiteDawnCache",
)
DATABASES = (
    "Cookies", "Network/Cookies", "QuotaManager", "WebStorage/QuotaManager",
    "Trust Tokens", "Network/Trust Tokens", "DIPS", "Device Bound Sessions",
    "Network/Device Bound Sessions", "Reporting and NEL", "Network/Reporting and NEL",
    "SharedStorage", "SharedStorage.db", "CdmStorage.db", "MediaDeviceSalts",
)
# WebStorage is removed as a unit, including its quota database and buckets.
PROFILE_TARGETS = frozenset(SITE_DIRS + CACHE_DIRS + tuple(
    name + suffix for name in DATABASES if not name.startswith("WebStorage/")
    for suffix in ("", "-journal", "-wal", "-shm")))


def windows_process_names():
    """Running executable names via one native Toolhelp snapshot (milliseconds, no PowerShell)."""
    from platforms.windows import process_names
    return process_names()


def chrome_running():
    """Check the browser and helpers, excluding the standalone crash reporter."""
    if sys.platform == "win32":
        # Called before every target during cleanup, so it must be cheap.
        try:
            return "chrome.exe" in windows_process_names()
        except OSError as exc:
            raise OSError("Cannot verify that Chrome is closed; cleanup was blocked") from exc
    cmd = ["pgrep", "-f", r"/Google Chrome[.]app/Contents/(MacOS/Google Chrome|Frameworks/.*/Google Chrome Helper)"]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OSError("Cannot verify that Chrome is closed; cleanup was blocked") from exc
    if result.returncode not in (0, 1):
        raise OSError("Cannot verify that Chrome is closed; cleanup was blocked")
    return result.returncode == 0


@dataclass(frozen=True)
class CleanupPlan:
    data_root: Path
    cache_root: Path
    profiles: tuple
    targets: tuple
    skipped: tuple


def safe_path(path):
    # Refuse symlinked targets and ancestors, including a redirected Chrome root.
    return not any(p.is_symlink() for p in (path, *path.parents))


def validate_target(plan, path):
    path = Path(path)
    if not safe_path(path):
        raise OSError("Refusing a symlinked cleanup path")
    if path == plan.cache_root:
        return
    if path.parent == plan.data_root and path.name in CACHE_DIRS:
        return
    try:
        parts = path.relative_to(plan.data_root).parts
    except ValueError as exc:
        raise OSError("Cleanup path is outside Chrome") from exc
    if len(parts) < 2 or parts[0] not in plan.profiles:
        raise OSError("Refusing to delete a Chrome profile or unrecognized path")
    if "/".join(parts[1:]) not in PROFILE_TARGETS:
        raise OSError("Cleanup path is not an approved site-data or cache location")


def build_plan(home=None):
    if home is not None:
        home_path = Path(home)
        mac_data = home_path / "Library/Application Support/Google/Chrome"
        win_data = home_path / "AppData/Local/Google/Chrome/User Data"
        if win_data.is_dir() and not mac_data.is_dir():
            data = win_data
            cache = home_path / "AppData/Local/Google/Chrome/Cache"
        else:
            data = mac_data
            cache = home_path / "Library/Caches/Google/Chrome"
    elif sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            data = Path(local_app_data) / "Google/Chrome/User Data"
            cache = Path(local_app_data) / "Google/Chrome/Cache"
        else:
            data = Path.home() / "AppData/Local/Google/Chrome/User Data"
            cache = Path.home() / "AppData/Local/Google/Chrome/Cache"
    else:
        home_path = Path.home()
        data = home_path / "Library/Application Support/Google/Chrome"
        cache = home_path / "Library/Caches/Google/Chrome"
    candidates, skipped, profiles = [], [], []
    for root in (data, cache):
        if not safe_path(root):
            raise OSError("Chrome data or cache directory uses a symlink; cleanup was blocked")
    if data.is_dir():
        for profile in sorted(data.iterdir()):
            if not PROFILE_NAME.fullmatch(profile.name):
                continue
            if not safe_path(profile):
                skipped.append(profile)
                continue
            if profile.is_dir():
                profiles.append(profile.name)
                candidates.extend(profile / relative for relative in sorted(PROFILE_TARGETS))
    # Chrome's separate macOS cache tree contains only disposable cache data.
    candidates.append(cache)
    plan = CleanupPlan(data, cache, tuple(profiles), (), ())
    targets = []
    for path in candidates:
        if not safe_path(path):
            if os.path.lexists(path):
                skipped.append(path)
            continue
        if os.path.lexists(path):
            validate_target(plan, path)
            targets.append(path)
    # Shared rendering caches live outside profile folders.
    for name in CACHE_DIRS:
        path = data / name
        if not safe_path(path):
            if os.path.lexists(path):
                skipped.append(path)
        elif path.is_dir():
            targets.append(path)
    return CleanupPlan(data, cache, tuple(profiles), tuple(targets), tuple(skipped))


class CleanupJob:
    def __init__(self, plan):
        self.plan = plan
        self.done = threading.Event()
        self.cancel = threading.Event()
        self.removed = []
        self.errors = []
        self.current = "Checking that Chrome is closed"
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            for path in self.plan.targets:
                if self.cancel.is_set():
                    self.errors.append("Cleanup cancelled; remaining locations were left untouched.")
                    break
                # Recheck during cleanup so relaunching Chrome stops further deletion.
                if chrome_running():
                    self.errors.append("Chrome is running. Quit Chrome (⌘Q) and try again.")
                    break
                self.current = str(path)
                try:
                    validate_target(self.plan, path)
                    if not os.path.lexists(path):
                        continue
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                    self.removed.append(path)
                except OSError as exc:
                    self.errors.append(f"{path}: {exc}")
        except Exception as exc:
            self.errors.append(str(exc))
        finally:
            self.done.set()
