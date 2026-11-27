#!/usr/bin/env python3
"""System Steward: native storage, cleanup and system maintenance."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any
import unicodedata

from app_identity import APP_NAME, AUTHOR, VERSION

GROUPS = ["All", "Browsers", "Creative", "Developer", "Projects", "Desktop", "Apps"]
# ⌘ is the macOS Command key; Windows users get a neutral code glyph instead.
CODE_ICON = "λ" if sys.platform == "win32" else "⌘"
GROUP_ICONS = {"All": "◉", "Browsers": "◎", "Creative": "✦", "Developer": CODE_ICON,
               "Projects": "▣", "Desktop": "▤", "Apps": "◇", "Folder contents": "▣"}
STATUS_ICONS = {"queued": "·", "scanning": "◐", "done": "✓", "partial": "△",
                "missing": "−", "unreadable": "△", "timed out": "◷", "cancelled": "×"}
FILE_ICONS = {
    **dict.fromkeys((".py", ".js", ".ts", ".jsx", ".tsx", ".sh", ".zsh", ".php", ".html", ".css", ".ps1", ".bat", ".cmd"), CODE_ICON),
    **dict.fromkeys((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".heic"), "▧"),
    **dict.fromkeys((".mp4", ".mov", ".mkv", ".webm"), "▷"),
    **dict.fromkeys((".mp3", ".wav", ".m4a", ".flac", ".aac"), "♫"),
    **dict.fromkeys((".zip", ".gz", ".tar", ".7z", ".dmg", ".iso", ".exe", ".msi"), "▥"),
    **dict.fromkeys((".md", ".txt", ".pdf", ".docx", ".json", ".yaml", ".toml"), "≡"),
}


@dataclass
class Entry:
    name: str
    path: str
    group: str
    size: int = 0
    status: str = "queued"


def clean(value: object) -> str:
    return "".join(c if c.isprintable() and not unicodedata.combining(c) else "?" for c in str(value))


def cell_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def clip_cells(text: object, width: int) -> str:
    result, cells = [], 0
    for char in clean(text):
        span = cell_width(char)
        if cells + span > width:
            break
        result.append(char)
        cells += span
    return "".join(result)


def entry_icon(entry: Entry, folder_view: bool) -> str:
    path = Path(entry.path)
    if path.suffix.lower() in (".app", ".exe"):
        return GROUP_ICONS["Apps"]
    if not folder_view:
        return GROUP_ICONS.get(entry.group, GROUP_ICONS["Folder contents"])
    if path.is_dir():
        return GROUP_ICONS["Folder contents"]
    return FILE_ICONS.get(path.suffix.lower(), "≡")


def pretty_path(path: str | os.PathLike[str]) -> str:
    home = str(Path.home())
    text = str(path)
    if text == home:
        return "~"
    if text.startswith(home + os.sep) or text.startswith(home + "/"):
        rel = text[len(home) + 1:].replace("\\", "/")
        return clean(f"~/{rel}")
    return clean(text.replace("\\", "/"))


def size_label(size: float) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024 or unit == "PiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024


def children(path: str | os.PathLike[str], group: str = "Projects") -> list[Entry]:
    with os.scandir(path) as listing:
        return [Entry(item.name, item.path, group) for item in listing if not item.is_symlink()]


def chrome_exe_windows(local_app_data: Path, prog_files: Path) -> Path:
    """Chrome installs per-user under LocalAppData or machine-wide under Program Files."""
    candidates = [
        local_app_data / "Google/Chrome/Application/chrome.exe",
        prog_files / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
    ]
    return next((path for path in candidates if path.is_file()), candidates[0])


def audit_entries_windows(home: Path) -> list[Entry]:
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData/Local"))
    app_data = Path(os.environ.get("APPDATA") or (home / "AppData/Roaming"))
    prog_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))

    specs = [
        ("Chrome", chrome_exe_windows(local_app_data, prog_files), "Browsers"),
        ("Chrome profiles & site data", local_app_data / "Google/Chrome/User Data", "Browsers"),
        ("Google browser cache", local_app_data / "Google/Chrome/User Data/Default/Cache", "Browsers"),
        ("Adobe · system support", prog_files / "Adobe", "Creative"),
        ("Adobe · user support", app_data / "Adobe", "Creative"),
        ("Build caches", home / ".cache", "Developer"),
        ("Scoop", home / "scoop", "Developer"),
        ("Codex", home / ".codex", "Developer"),
        ("pnpm", local_app_data / "pnpm", "Developer"),
        ("VS Code", home / ".vscode", "Developer"),
        ("npm cache", local_app_data / "npm-cache", "Developer"),
        ("Gemini", home / ".gemini", "Developer"),
        ("Microsoft Office", prog_files / "Microsoft Office", "Apps"),
        ("Microsoft support", local_app_data / "Microsoft", "Apps"),
        ("WhatsApp", local_app_data / "WhatsApp", "Apps"),
        ("WhatsApp packages", local_app_data / "Packages", "Apps"),
        ("Laravel Herd", home / ".config/herd", "Apps"),
    ]
    entries = [Entry(n, str(p), g) for n, p, g in specs]
    for folder, group, predicate in [
        (prog_files, "Creative", lambda n: n.startswith("Adobe")),
        (home / "Herd", "Projects", lambda n: True),
        (home / "projects", "Projects", lambda n: True),
        (home / "sites", "Projects", lambda n: True),
        (home / "source/repos", "Projects", lambda n: True),
        (home / "Desktop", "Desktop", lambda n: "deploy" in n.lower() or "setup" in n.lower()),
        (local_app_data / "Adobe", "Creative", lambda n: True),
    ]:
        try:
            if folder.exists():
                entries.extend(e for e in children(folder, group) if predicate(e.name))
        except OSError as exc:
            entries.append(Entry(folder.name, str(folder), group, status=f"unavailable: {exc.strerror}"))
    seen = set()
    unique = []
    for e in entries:
        norm = os.path.normcase(os.path.abspath(e.path))
        if norm not in seen:
            seen.add(norm)
            unique.append(e)
    return unique


def audit_entries() -> list[Entry]:
    home = Path.home()
    if sys.platform == "win32":
        return audit_entries_windows(home)
    specs = [
        ("Chrome", "/Applications/Google Chrome.app", "Browsers"),
        ("Chrome profiles & site data", home / "Library/Application Support/Google/Chrome", "Browsers"),
        ("Google browser cache", home / "Library/Caches/Google", "Browsers"),
        ("Adobe · system support", "/Library/Application Support/Adobe", "Creative"),
        ("Adobe · user support", home / "Library/Application Support/Adobe", "Creative"),
        ("Build caches", home / ".cache", "Developer"),
        ("Homebrew", "/opt/homebrew", "Developer"),
        ("Codex", home / ".codex", "Developer"),
        ("pnpm", home / "Library/pnpm", "Developer"),
        ("VS Code", home / ".vscode", "Developer"),
        ("npm cache", home / ".npm", "Developer"),
        ("Gemini", home / ".gemini", "Developer"),
        ("Microsoft Word", "/Applications/Microsoft Word.app", "Apps"),
        ("Microsoft support", home / "Library/Application Support/Microsoft", "Apps"),
        ("WhatsApp", "/Applications/WhatsApp.app", "Apps"),
        ("WhatsApp media", home / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared", "Apps"),
        ("Laravel Herd", home / "Library/Application Support/Herd", "Apps"),
    ]
    entries = [Entry(n, str(p), g) for n, p, g in specs]
    for folder, group, predicate in [
        (Path("/Applications"), "Creative", lambda n: n.startswith("Adobe")),
        (home / "sites", "Projects", lambda n: True),
        (home / "Desktop", "Desktop", lambda n: "deploy" in n.lower()),
        (home / "Library/Caches", "Creative", lambda n: n in ("Adobe", "Adobe InDesign")
         or n.startswith(("Adobe Camera Raw", "com.adobe."))),
    ]:
        try:
            entries.extend(e for e in children(folder, group) if predicate(e.name))
        except OSError as exc:
            entries.append(Entry(folder.name, str(folder), group, status=f"unavailable: {exc.strerror}"))
    return entries


def du_command(path: str | os.PathLike[str]) -> list[str]:
    if sys.platform != "win32":
        du_bin = shutil.which("du")
        if du_bin:
            return [du_bin, "-sk", str(path)]
    code = (
        "import os, sys\n"
        "p = sys.argv[1]\n"
        "total = 0\n"
        "try:\n"
        "    if os.path.isfile(p) or os.path.islink(p):\n"
        "        try: total = os.lstat(p).st_size\n"
        "        except OSError: pass\n"
        "    else:\n"
        "        for r, dirs, files in os.walk(p, followlinks=False):\n"
        "            try:\n"
        "                with os.scandir(r) as it:\n"
        "                    for e in it:\n"
        "                        if e.is_file(follow_symlinks=False):\n"
        "                            try: total += e.stat(follow_symlinks=False).st_size\n"
        "                            except OSError: pass\n"
        "            except OSError: pass\n"
        "except Exception: pass\n"
        "kb = (total + 1023) // 1024\n"
        "print(f'{kb}\\t{p}')\n"
    )
    return [sys.executable, "-c", code, str(path)]


FILE_ATTRIBUTE_SPARSE_FILE = 0x00000200
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_COMPRESSED = 0x00000800
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
# OneDrive / cloud placeholders report their full size but occupy no local clusters.
CLOUD_ONLY = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
# NTFS-compressed, sparse and WOF-compacted files can occupy much less than their logical size.
ON_DISK_SIZE_DIFFERS = FILE_ATTRIBUTE_SPARSE_FILE | FILE_ATTRIBUTE_COMPRESSED | FILE_ATTRIBUTE_REPARSE_POINT
CHECK_EVERY = 64  # directories between cancellation / deadline checks


def windows_cluster_size(path: str | os.PathLike[str]) -> int:
    """Allocation unit of the volume holding ``path`` (4 KiB off Windows)."""
    if sys.platform != "win32":
        return 4096
    from platforms.windows import cluster_size
    return cluster_size(path)


def windows_on_disk_size(path: str, fallback: int) -> int:
    if sys.platform != "win32":
        return fallback
    from platforms.windows import on_disk_size
    try:
        return on_disk_size(path, fallback)
    except OSError:
        return fallback


def allocated_bytes(stat: os.stat_result, path: str, cluster: int) -> int:
    """Disk space a file occupies, rounded to whole clusters like ``du`` on macOS."""
    attributes = getattr(stat, "st_file_attributes", 0)
    if attributes & CLOUD_ONLY:
        return 0
    size = stat.st_size
    if attributes & ON_DISK_SIZE_DIFFERS:
        size = windows_on_disk_size(path, size)
    return -(-size // cluster) * cluster


def tree_usage(path: str, cancel: threading.Event | None = None, deadline: float | None = None) -> tuple[int, str]:
    """Measure a file or folder in-process with an iterative scandir walk.

    Never follows symlinks or junctions, so a link cannot redirect or double-count the scan.
    Returns ``(bytes, status)`` where status is done, partial, cancelled or timed out.
    """
    cluster = windows_cluster_size(path)
    try:
        info = os.lstat(path)
    except OSError:
        return 0, "unreadable"
    if not os.path.isdir(path) or os.path.islink(path):
        return (0 if os.path.islink(path) else allocated_bytes(info, path, cluster)), "done"
    total, complete, visited = 0, True, 0
    stack = [path]
    while stack:
        visited += 1
        if visited % CHECK_EVERY == 0:
            if cancel is not None and cancel.is_set():
                return total, "cancelled"
            if deadline is not None and time.monotonic() > deadline:
                return total, "timed out"
        try:
            listing = os.scandir(stack.pop())
        except OSError:
            complete = False
            continue
        with listing:
            for item in listing:
                try:
                    if item.is_symlink() or item.is_junction():
                        continue
                    if item.is_dir(follow_symlinks=False):
                        stack.append(item.path)
                    else:
                        # On Windows DirEntry.stat() comes from the directory listing: no extra syscall.
                        total += allocated_bytes(item.stat(follow_symlinks=False), item.path, cluster)
                except OSError:
                    complete = False
    return total, "done" if complete else "partial"


class Scan:
    def __init__(self, entries: list[Entry], timeout: float = 120, in_process: bool | None = None) -> None:
        self.entries, self.timeout = entries, timeout
        # Windows measures inside this process (no interpreter per folder); macOS keeps `du`.
        self.in_process = sys.platform == "win32" if in_process is None else in_process
        self.cancel = threading.Event()
        self.finished = threading.Event()
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def measure(self, entry: Entry) -> None:
        if self.cancel.is_set() or entry.status != "queued":
            return
        entry.status = "scanning"
        if not os.path.lexists(entry.path):
            entry.status = "missing"
            return
        if self.in_process:
            size, status = tree_usage(entry.path, self.cancel, time.monotonic() + self.timeout)
            entry.size = size
            entry.status = status
            return
        process = None
        try:
            cmd = du_command(entry.path)
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True, errors="replace")
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    output, _ = process.communicate(timeout=0.15)
                    break
                except subprocess.TimeoutExpired:
                    if self.cancel.is_set() or time.monotonic() > deadline:
                        process.kill()
                        process.communicate()
                        entry.status = "cancelled" if self.cancel.is_set() else "timed out"
                        return
            if output.strip():
                entry.size = int(output.split()[0]) * 1024
                entry.status = "done" if process.returncode == 0 else "partial"
            else:
                entry.status = "unreadable"
        except (OSError, ValueError):
            entry.status = "unreadable"
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()

    def run(self) -> None:
        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(self.measure, self.entries))
        finally:
            self.finished.set()

    def close(self) -> None:
        self.cancel.set()
        self.thread.join(timeout=2)


def report(scan: Scan, root: Path | None) -> dict[str, Any]:
    platform_label = "curated Windows audit" if sys.platform == "win32" else "curated macOS audit"
    return {"created_at": datetime.datetime.now().astimezone().isoformat(),
            "scope": str(root) if root else platform_label,
            "measurement": "allocated bytes; partial scans may undercount",
            "entries": [asdict(e) for e in scan.entries]}



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION} · by {AUTHOR}")
    parser.add_argument("path", nargs="?", type=Path, help="explore a folder instead of the system audit")
    parser.add_argument("--json", action="store_true", help="print a JSON report without opening the UI")
    parser.add_argument("--timeout", type=float, default=120, help="maximum seconds per location (default: 120)")
    parser.add_argument("-k", "--keep-awake", "--awake", action="store_true", help="start Keep Awake in background and exit")
    parser.add_argument("--stop-awake", action="store_true", help="stop active Keep Awake background session")
    parser.add_argument("--status-awake", action="store_true", help="check Keep Awake background status")
    parser.add_argument("--display", action="store_true", help="prevent display sleep in addition to system sleep "
                        "(the default on Windows Modern Standby PCs)")
    parser.add_argument("--allow-display-sleep", action="store_true",
                        help="let the display sleep even on a Modern Standby PC (it may then enter standby)")
    parser.add_argument("-t", "--duration", type=int, default=None, help="timer duration in seconds for keep awake")
    parser.add_argument("--update", action="store_true",
                        help="update now through Scoop, winget or Homebrew if a newer version is available")
    args = parser.parse_args()

    if args.update:
        import updater
        channel = updater.detect_channel()
        if channel is None:
            print("This copy was not installed with Scoop, winget or Homebrew, so it cannot update itself.\n"
                  "Install page: https://rajoyish.github.io/system-steward/")
            return 0
        update = updater.check(channel, force=True)
        if update is None:
            print(f"{APP_NAME} {VERSION} is the newest version {channel.label} offers.")
            return 0
        return updater.apply(update, ["--version"])

    if args.stop_awake:
        from keep_awake import AwakeSession
        session = AwakeSession()
        if not session.poll():
            print("No active Keep Awake session found.")
            return 0
        session.stop()
        print("✓ Keep Awake session stopped.")
        return 0

    if args.status_awake:
        from keep_awake import AwakeSession
        session = AwakeSession()
        session.poll()
        print(session.status())
        return 0

    if args.keep_awake:
        from keep_awake import AwakeSession, display_required, plan_session, standby_warning, stop_hint
        if args.display and args.allow_display_sleep:
            parser.error("--display and --allow-display-sleep cannot be combined")
        session = AwakeSession()
        if session.poll():
            print(f"Keep Awake is already active: {session.status()}")
            return 0
        flags = ["i"]
        if args.display or (display_required() and not args.allow_display_sleep):
            flags.append("d")
        dur = str(args.duration) if args.duration else ""
        try:
            plan = plan_session(flags=flags, mode="manual", duration=dur, background=True)
            session.start(plan)
        except (ValueError, RuntimeError, OSError) as exc:
            print(f"System Steward: {exc}", file=sys.stderr)
            return 1
        print(f"✓ {session.status()}")
        if "d" in flags and not args.display:
            print("  Display: kept on, because this PC uses Modern Standby (--allow-display-sleep lets it sleep).")
        warning = standby_warning("".join(flags))
        if warning:
            print(f"  Warning: {warning}")
        print("  Persistence: Keeps running after this terminal closes; ends when the computer shuts down.")
        print("  Not covered: closing the lid or choosing Sleep yourself still puts the PC to sleep.")
        print(f"  To stop: {stop_hint()}")
        return 0

    if not 0 < args.timeout < float("inf"):
        parser.error("--timeout must be a finite number greater than zero")
    if args.path:
        args.path = args.path.expanduser().resolve()
        if not args.path.is_dir():
            parser.error(f"not a directory: {args.path}")
    try:
        if args.json:
            scan = Scan(children(args.path) if args.path else audit_entries(), args.timeout)
            try:
                scan.finished.wait()
                print(json.dumps(report(scan, args.path), indent=2))
            finally:
                scan.close()
        elif not sys.stdin.isatty() or not sys.stdout.isatty():
            parser.error("the interactive UI needs a terminal; use --json for scripts")
        else:
            try:
                from system_steward_tui import SystemStewardApp
            except ModuleNotFoundError as exc:
                if exc.name not in ("textual", "rich"):
                    raise
                print("Install the terminal UI with ./setup, then run ./system-steward", file=sys.stderr)
                return 1
            app = SystemStewardApp(root=args.path, timeout=args.timeout, check_updates=True)
            app.run()
            if app.pending_update is not None:
                import updater
                return updater.apply(app.pending_update, sys.argv[1:])
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"System Steward: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
