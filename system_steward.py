#!/usr/bin/env python3
"""System Steward: native storage, cleanup and system maintenance."""
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from app_identity import APP_NAME, AUTHOR, VERSION
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

GROUPS = ["All", "Browsers", "Creative", "Developer", "Projects", "Desktop", "Apps"]
GROUP_ICONS = {"All": "◉", "Browsers": "◎", "Creative": "✦", "Developer": "⌘",
               "Projects": "▣", "Desktop": "▤", "Apps": "◇", "Folder contents": "▣"}
STATUS_ICONS = {"queued": "·", "scanning": "◐", "done": "✓", "partial": "△",
                "missing": "−", "unreadable": "△", "timed out": "◷", "cancelled": "×"}
FILE_ICONS = {
    **dict.fromkeys((".py", ".js", ".ts", ".jsx", ".tsx", ".sh", ".zsh", ".php", ".html", ".css", ".ps1", ".bat", ".cmd"), "⌘"),
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


def clean(value):
    return "".join(c if c.isprintable() and not unicodedata.combining(c) else "?" for c in str(value))


def cell_width(text):
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def clip_cells(text, width):
    result, cells = [], 0
    for char in clean(text):
        span = cell_width(char)
        if cells + span > width:
            break
        result.append(char)
        cells += span
    return "".join(result)


def entry_icon(entry, folder_view):
    path = Path(entry.path)
    if path.suffix.lower() in (".app", ".exe"):
        return GROUP_ICONS["Apps"]
    if not folder_view:
        return GROUP_ICONS.get(entry.group, GROUP_ICONS["Folder contents"])
    if path.is_dir():
        return GROUP_ICONS["Folder contents"]
    return FILE_ICONS.get(path.suffix.lower(), "≡")


def pretty_path(path):
    home = str(Path.home())
    text = str(path)
    if text == home:
        return "~"
    if text.startswith(home + os.sep) or text.startswith(home + "/"):
        rel = text[len(home) + 1:].replace("\\", "/")
        return clean(f"~/{rel}")
    return clean(text.replace("\\", "/"))


def size_label(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024 or unit == "PiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024


def children(path, group="Projects"):
    with os.scandir(path) as listing:
        return [Entry(item.name, item.path, group) for item in listing if not item.is_symlink()]


def audit_entries_windows(home):
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData/Local"))
    app_data = Path(os.environ.get("APPDATA") or (home / "AppData/Roaming"))
    prog_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    prog_files_x86 = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))

    specs = [
        ("Chrome", local_app_data / "Google/Chrome/Application/chrome.exe", "Browsers"),
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


def audit_entries():
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


def du_command(path):
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


class Scan:
    def __init__(self, entries, timeout=120):
        self.entries, self.timeout = entries, timeout
        self.cancel = threading.Event()
        self.finished = threading.Event()
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def measure(self, entry):
        if self.cancel.is_set() or entry.status != "queued":
            return
        entry.status = "scanning"
        if not os.path.lexists(entry.path):
            entry.status = "missing"
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

    def run(self):
        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(self.measure, self.entries))
        finally:
            self.finished.set()

    def close(self):
        self.cancel.set()
        self.thread.join(timeout=2)


def report(scan, root):
    platform_label = "curated Windows audit" if sys.platform == "win32" else "curated macOS audit"
    return {"created_at": datetime.datetime.now().astimezone().isoformat(),
            "scope": str(root) if root else platform_label,
            "measurement": "allocated bytes; partial scans may undercount",
            "entries": [asdict(e) for e in scan.entries]}



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION} · by {AUTHOR}")
    parser.add_argument("path", nargs="?", type=Path, help="explore a folder instead of the system audit")
    parser.add_argument("--json", action="store_true", help="print a JSON report without opening the UI")
    parser.add_argument("--timeout", type=float, default=120, help="maximum seconds per location (default: 120)")
    parser.add_argument("-k", "--keep-awake", "--awake", action="store_true", help="start Keep Awake in background and exit")
    parser.add_argument("--stop-awake", action="store_true", help="stop active Keep Awake background session")
    parser.add_argument("--status-awake", action="store_true", help="check Keep Awake background status")
    parser.add_argument("--display", action="store_true", help="prevent display sleep in addition to system sleep")
    parser.add_argument("-t", "--duration", type=int, default=None, help="timer duration in seconds for keep awake")
    args = parser.parse_args()

    if args.stop_awake:
        from keep_awake import AwakeSession
        session = AwakeSession()
        if not session.running:
            print("No active Keep Awake session found.")
            return 0
        session.stop()
        print("✓ Keep Awake session stopped.")
        return 0

    if args.status_awake:
        from keep_awake import AwakeSession
        session = AwakeSession()
        print(session.status())
        return 0

    if args.keep_awake:
        from keep_awake import AwakeSession, plan_session
        session = AwakeSession()
        if session.running:
            print(f"Keep Awake is already active: {session.status()}")
            return 0
        flags = ["i"]
        if args.display:
            flags.append("d")
        dur = str(args.duration) if args.duration else ""
        plan = plan_session(flags=flags, mode="manual", duration=dur, background=True)
        session.start(plan)
        print(f"✓ {session.status()}")
        print("  Memory: ~2-4 MB (optimized, 99% less than terminal TUI).")
        print("  Persistence: Keeps running even if terminal closes.")
        print("  Shutdown: Turns off automatically when computer turns off.")
        print("  To stop: pwsh -File .\\keep-awake.ps1 -Stop  (or --stop-awake)")
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
            SystemStewardApp(root=args.path, timeout=args.timeout).run()
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"System Steward: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
