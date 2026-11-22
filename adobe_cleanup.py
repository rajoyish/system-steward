"""Clear approved Adobe cache contents without deleting cache parent folders."""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import NoReturn

from chrome_cleanup import safe_path

COMMON_CACHES = frozenset(("Media Cache", "Media Cache Files", "Peak Files"))
PROTECTED_NAMES = frozenset(("presets", "plugins", "plug-ins", "scripts", "preferences",
                             "workspaces", "workspace", "catalogs", "catalogues"))
DISK_CACHE = re.compile(
    r"(?:(?:.*[ _-])?disk[ _-]*cache|caches?|temp(?:orary)?(?:[ _-]*(?:cache|files|items))?)(?:[ ._-].*)?\Z", re.I)
ADOBE_PROCESS = re.compile(
    r"(?:^|[/\\])(?:Adobe[^/\\]*|Creative Cloud[^/\\]*|Lightroom[^/\\]*|Photoshop[^/\\]*|"
    r"Illustrator[^/\\]*|InDesign[^/\\]*|After Effects[^/\\]*|Premiere[^/\\]*|Audition[^/\\]*|"
    r"Media Encoder[^/\\]*|Bridge|CCXProcess|Core ?Sync|CCLibrary)(?:[/\\]|\.exe|$)", re.I)

if sys.platform == "win32":
    QUIT_LINES = ("Exit with File > Exit, then relaunch before resuming work.",
                  "Save work and exit every Adobe app (File > Exit).",
                  "Quit Creative Cloud from its system-tray icon menu too.",
                  "Confirm in Task Manager that Adobe apps are closed.")
else:
    QUIT_LINES = ("Quit with Command-Q, then relaunch before resuming work.",
                  "Save work and quit every Adobe app with Command-Q.",
                  "Quit Creative Cloud from its menu-bar account menu too.",
                  "Confirm in Activity Monitor that Adobe apps are closed.")

# Short lines keep the in-app guide readable in a 68-column terminal.
PURGE_GUIDE = (
    "SAFEST FIRST: PURGE INSIDE THE APP",
    "Save your work; close all projects, timelines and compositions.",
    "Premiere / Media Encoder: Settings > Media Cache > Delete.",
    "Choose all media cache files (no project open), then confirm.",
    "After Effects: Settings > Media & Disk Cache (or Disk).",
    "Use Empty Disk Cache and Clean Database & Cache if available.",
    "Lightroom Classic: Settings > Performance > Purge Cache",
    "under Camera Raw Cache Settings. Labels vary by version.",
    "Verify the cache size resets or the purge finishes cleanly.",
    QUIT_LINES[0],
    "",
    "MANUAL CLEANUP WITH THIS APP",
    *QUIT_LINES[1:],
    "Esc returns to the preview. Select the cache groups to clear.",
    "Choose Review selection, inspect the targets, then click Permanently clear or press Y.",
    "Cache parent folders stay. Presets and catalogs are protected.",
    "Deletion is permanent; nothing goes to Trash / the Recycle Bin.",
    "Relaunch Adobe and verify projects reopen and caches rebuild.",
    "",
    "KEEP CACHE GROWTH UNDER CONTROL",
    "Premiere: Media Cache > automatic size-based deletion.",
    "After Effects: Media & Disk Cache / Disk > maximum size.",
    "These settings can also move caches to a fast external SSD.",
    "Custom / external cache paths are not deleted by this app.",
)


def protected_name(name: str) -> bool:
    lowered = name.casefold()
    return ".lrcat" in lowered or lowered in PROTECTED_NAMES


def protected_tree(path: Path) -> bool:
    """If a candidate contains protected files, keep that entire candidate."""
    if protected_name(path.name) or not safe_path(path):
        return True
    if path.is_dir():
        def failed(error: OSError) -> NoReturn:
            raise error
        for current, dirs, files in os.walk(path, followlinks=False, onerror=failed):
            for name in dirs + files:
                if protected_name(name) or (Path(current) / name).is_symlink():
                    return True
    return False


def running_process_names() -> list[str]:
    """Executable names (Windows) or command paths (macOS) of running processes."""
    blocked = "Cannot verify that Adobe apps are closed; cleanup was blocked"
    if sys.platform == "win32":
        try:
            from chrome_cleanup import windows_process_names
            return windows_process_names()
        except OSError as exc:
            raise OSError(blocked) from exc
    try:
        result = subprocess.run(["ps", "-axo", "comm="], capture_output=True,
                                text=True, errors="replace", timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OSError(blocked) from exc
    if result.returncode:
        raise OSError(blocked)
    return result.stdout.splitlines()


def adobe_running() -> bool:
    return any(ADOBE_PROCESS.search(line.strip()) for line in running_process_names())


def approved_cache(library: Path, path: Path) -> bool:
    try:
        parts = path.relative_to(library).parts
    except ValueError:
        return False
    if any(protected_name(part) for part in parts):
        return False
    if len(parts) == 4 and parts[:3] == ("Application Support", "Adobe", "Common"):
        return parts[3] in COMMON_CACHES
    if len(parts) >= 2 and parts[-2] == "Common" and ("Adobe" in parts or (len(parts) >= 3 and parts[-3] == "Adobe")):
        return parts[-1] in COMMON_CACHES
    if len(parts) == 2 and parts[0] == "Caches":
        return (parts[1] == "Adobe InDesign" or
                bool(re.fullmatch(r"Adobe Camera Raw(?: \d+)?", parts[1])) or
                parts[1].startswith("com.adobe.") and len(parts[1]) > len("com.adobe."))
    if len(parts) >= 3 and parts[:2] == ("Caches", "Adobe"):
        return bool(DISK_CACHE.fullmatch(parts[-1])) or (
            len(parts) >= 4 and parts[2].casefold() in ("lightroom", "lightroom classic")
            and parts[-1].casefold() == "video")
    if "Adobe" in parts:
        if bool(DISK_CACHE.fullmatch(parts[-1])):
            return True
        if parts[-1] in COMMON_CACHES:
            return True
        if parts[-1].casefold() == "video" and any(p.casefold() in ("lightroom", "lightroom classic") for p in parts):
            return True
        if parts[-1] in ("InDesign", "Adobe InDesign") or bool(re.fullmatch(r"Adobe Camera Raw(?: \d+)?", parts[-1])):
            return True
    return False


@dataclass(frozen=True)
class AdobePlan:
    data_root: Path
    cache_dirs: tuple
    targets: tuple
    skipped: tuple


def build_plan(home: str | os.PathLike[str] | None = None) -> AdobePlan:
    if home is not None:
        home_path = Path(home)
        library = home_path / "Library"
        win_appdata = home_path / "AppData/Roaming/Adobe"
        if win_appdata.is_dir() and not library.is_dir():
            data_root = home_path
            common = win_appdata / "Common"
            caches = home_path / "AppData/Local/Adobe"
        else:
            data_root = library
            common = library / "Application Support/Adobe/Common"
            caches = library / "Caches"
    elif sys.platform == "win32":
        data_root = Path.home()
        roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        common = roaming / "Adobe/Common"
        caches = local / "Adobe"
    else:
        library = Path.home() / "Library"
        data_root = library
        common = library / "Application Support/Adobe/Common"
        caches = library / "Caches"

    candidates = [common / name for name in sorted(COMMON_CACHES)]
    skipped = []
    if not safe_path(data_root) or (caches.exists() and not safe_path(caches)) or (common.exists() and not safe_path(common)):
        raise OSError("Adobe cache roots use a symlink; cleanup was blocked")
    if caches.is_dir():
        candidates.extend(path for path in sorted(caches.iterdir()) if approved_cache(data_root, path))
    adobe = caches if caches.name == "Adobe" else caches / "Adobe"
    if adobe.is_symlink():
        skipped.append(adobe)
    elif adobe.is_dir():
        def failed(error: OSError) -> NoReturn:
            raise error
        for current, dirs, _ in os.walk(adobe, followlinks=False, onerror=failed):
            for name in dirs[:]:
                path = Path(current) / name
                if path.is_symlink() or protected_name(name):
                    skipped.append(path)
                    dirs.remove(name)
                elif approved_cache(data_root, path):
                    candidates.append(path)
                    dirs.remove(name)
    roots, targets = [], []
    for cache in sorted(set(candidates)):
        if not safe_path(cache):
            skipped.append(cache)
            continue
        if not cache.is_dir():
            continue
        roots.append(cache)
        for target in sorted(cache.iterdir()):
            if protected_tree(target):
                skipped.append(target)
            else:
                targets.append(target)
    return AdobePlan(data_root, tuple(roots), tuple(targets), tuple(skipped))


def validate_target(plan: AdobePlan, path: Path) -> None:
    if path.parent not in plan.cache_dirs or not approved_cache(plan.data_root, path.parent):
        raise OSError("Refusing to delete an Adobe parent folder or unapproved location")
    if not safe_path(path) or protected_tree(path):
        raise OSError("Refusing a protected catalog, settings folder, or symlinked path")


class AdobeCleanupJob:
    def __init__(self, plan: AdobePlan) -> None:
        self.plan = plan
        self.done, self.cancel = threading.Event(), threading.Event()
        self.removed, self.errors = [], []
        self.current = "Checking that Adobe apps are closed"
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self) -> None:
        try:
            checked_at = None
            for path in self.plan.targets:
                if self.cancel.is_set():
                    self.errors.append("Cleanup cancelled; remaining cache contents were left untouched.")
                    break
                if checked_at is None or time.monotonic() - checked_at >= .5:
                    if adobe_running():
                        self.errors.append("Adobe apps or helpers are running. Quit Adobe and Creative Cloud, then retry.")
                        break
                    checked_at = time.monotonic()
                self.current = str(path)
                try:
                    if not os.path.lexists(path):
                        continue
                    validate_target(self.plan, path)
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
