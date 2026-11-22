"""Native macOS and Windows maintenance discovery and presentation."""
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from system_steward import size_label

FEATURES = (
    ("System Status", "status"), ("Clean System", "clean"),
    ("Optimize System", "optimize"), ("Purge Dev Artifacts", "purge"),
    ("Clean Installers", "installer"), ("Uninstall App", "uninstall"),
    ("Touch ID for Sudo", "touchid"),
    ("Keep Awake (macOS)", "awake"),
)
ARTIFACTS = {"node_modules", ".next", ".nuxt", ".venv", "venv", "target", "dist", "build"}
MANIFESTS = {"package.json", "Cargo.toml", "pyproject.toml", "requirements.txt", "go.mod", "composer.json", "pom.xml", "build.gradle"}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")


@dataclass(frozen=True)
class Candidate:
    label: str
    targets: tuple[Path, ...]
    identities: tuple[tuple[int, int], ...]
    effect: str


def candidate(label, targets, effect):
    paths = tuple(Path(p).absolute() for p in targets)
    for path in paths:
        # Refuse redirected ancestors as well as a symlink at the target itself.
        if path.is_symlink() or path.resolve() != path:
            raise ValueError(f"Redirected path: {path}")
    if not paths:
        raise ValueError("Cleanup group is empty")
    return Candidate(label, paths, tuple((p.stat().st_dev, p.stat().st_ino) for p in paths), effect)


def discover(kind, home=None, project_root=None, cancel=None):
    home = Path(home or Path.home()).resolve()
    groups, warnings = [], []

    def add(label, paths, effect):
        try:
            groups.append(candidate(label, paths, effect))
        except (OSError, ValueError) as exc:
            warnings.append(str(exc))

    def items(root):
        if root.is_symlink() or root.resolve() != root:
            warnings.append(f"Skipped redirected folder: {root}")
            return []
        try:
            return sorted(root.iterdir()) if root.exists() else []
        except OSError as exc:
            warnings.append(str(exc))
            return []

    if kind == "clean":
        clean_roots = []
        if sys.platform == "win32" and home == Path.home().resolve():
            local_app = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData/Local"))
            clean_roots.extend([
                (local_app / "Temp", "Temporary files"),
                (local_app / "CrashDumps", "Crash dumps"),
                (home / ".cache", "Developer cache"),
            ])
        clean_roots.extend([
            (home / "Library/Caches", "App cache"),
            (home / "Library/Logs", "App logs"),
            (home / ".cache", "Developer cache"),
        ])
        seen_roots = set()
        for root, label in clean_roots:
            if root in seen_roots:
                continue
            seen_roots.add(root)
            for path in items(root):
                add(f"{label} · {path.name}", [path], "Move this cache or log folder to Trash. Quit the related app first; caches rebuild on next launch.")
    elif kind in ("installer", "purge"):
        roots = [home / name for name in ("Downloads", "Desktop", "Documents")] if kind == "installer" else [Path(project_root or home / "sites").expanduser().absolute()]
        for root in roots:
            if not root.exists():
                continue
            if root.resolve() != root or root.is_symlink():
                warnings.append(f"Skipped redirected scan root: {root}")
                continue
            def walk_error(exc):
                warnings.append(str(exc))
            for current, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
                if cancel and cancel.is_set():
                    return groups, warnings
                parent = Path(current)
                dirs[:] = sorted(d for d in dirs if d not in {".git", ".Trash", ".svn"} and not (parent / d).is_symlink())
                if kind == "purge":
                    project = bool(MANIFESTS.intersection(files))
                    for name in list(dirs):
                        if name in ARTIFACTS and (project or name in {"node_modules", ".next", ".nuxt", ".venv"}):
                            path = parent / name
                            add(str(path.relative_to(root)), [path], "Move generated dependencies or build output to Trash. Stop builds and dev servers first; reinstall dependencies or rebuild afterward.")
                            dirs.remove(name)
                else:
                    # Do not descend into applications, projects' dependency stores, or hidden data.
                    dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ARTIFACTS and not d.endswith(".app")]
                    for name in sorted(files):
                        if Path(name).suffix.lower() in {".dmg", ".pkg", ".iso", ".exe", ".msi"}:
                            add(name, [parent / name], "Move this downloaded installer to Trash. Installed applications are kept.")
    elif kind == "uninstall":
        roots = [home / "Applications"]
        if home == Path.home().resolve():
            roots.insert(0, Path("/Applications"))
        for root in roots:
            for path in items(root):
                if path.suffix != ".app" or not path.is_dir():
                    continue
                targets = [path]
                try:
                    bundle = ""
                    plist_path = path / "Contents/Info.plist"
                    if plist_path.is_file():
                        try:
                            import plistlib
                            with open(plist_path, "rb") as f:
                                data = plistlib.load(f)
                                bundle = data.get("CFBundleIdentifier", "")
                        except Exception:
                            pass
                    if not bundle and sys.platform == "darwin":
                        result = subprocess.run(["/usr/bin/plutil", "-convert", "json", "-o", "-", str(plist_path)],
                            capture_output=True, text=True, timeout=5, check=True)
                        bundle = json.loads(result.stdout).get("CFBundleIdentifier", "")
                    if bundle and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]+", bundle):
                        for base, name in (("Caches", bundle), ("Preferences", bundle + ".plist"), ("Application Support", bundle), ("Saved Application State", bundle + ".savedState"), ("Logs", bundle)):
                            residual = home / "Library" / base / name
                            if residual.exists():
                                targets.append(residual)
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
                add(path.stem, targets, "Move this app and the listed exact bundle-ID residuals to Trash. Quit the app first. Shared data, containers and unmatched residuals are kept.")
    else:
        raise ValueError(f"Unknown discovery feature: {kind}")
    return groups, warnings


RECYCLE_BIN = "Recycle Bin"


def recycle_path(path):
    """Send one reviewed path to the Windows Recycle Bin (see platforms.windows.recycle)."""
    path = Path(path)
    if not path.is_absolute() or any(c in str(path) for c in "*?"):
        raise ValueError(f"Refusing an unexpected Recycle Bin path: {path}")
    from platforms.windows import recycle
    recycle(path)

def trash_group(group, home=None, moved=None, backend="auto"):
    """Only move the reviewed targets, refusing replacements and redirected paths.

    ``backend``: "auto" uses the Recycle Bin on Windows and ``~/.Trash`` elsewhere; "folder" forces
    the ``home/.Trash`` folder; a callable receives each path (used by tests).
    """
    home = Path(home or Path.home()).resolve()
    recycle = recycle_path if backend == "auto" and sys.platform == "win32" else backend if callable(backend) else None
    trash = home / ".Trash"
    if recycle is None and (trash.is_symlink() or trash.resolve() != trash):
        raise ValueError("Trash is redirected")
    if not group.targets or len(group.targets) != len(group.identities):
        raise ValueError("Invalid cleanup group")
    for path, identity in zip(group.targets, group.identities):
        if path.is_symlink() or path.resolve() != path or (path.stat().st_dev, path.stat().st_ino) != identity:
            raise ValueError(f"Target changed since preview: {path}")
    moved = moved if moved is not None else []
    if recycle is not None:
        for path in group.targets:
            recycle(path)
            moved.append((path, RECYCLE_BIN))
        return moved
    trash.mkdir(mode=0o700, exist_ok=True)
    for path in group.targets:
        # Unique destination preserves earlier trashed items; rename never traverses contents.
        destination = trash / f"{path.name}.{uuid.uuid4().hex}"
        try:
            path.rename(destination)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise ValueError(f"{path} is on a different volume than {trash}; move it to that volume's Trash in Finder") from exc
            raise
        moved.append((path, destination))
    return moved


def status_rows(data):
    """Present native system samples, preserving unavailable readings."""
    def formatted(key, value):
        if value is None:
            return "Unavailable"
        if key in {"used", "total", "compressed", "cached", "received", "sent", "total_ram"} and isinstance(value, (int, float)):
            return size_label(value)
        if isinstance(value, float):
            return f"{value:.2f}"
        return str(value)

    def details(item):
        return "\n".join(f"{key.replace('_', ' ').capitalize()}: {formatted(key, value)}" for key, value in item.items())

    score = data.get("health_score")
    rows = [("Resource score", f"{score}/100" if score is not None else "Unavailable", data.get("health_score_msg", ""))]
    for name, key in (("CPU", "cpu"), ("Memory", "memory")):
        item = data.get(key) or {}
        percent = item.get("usage" if key == "cpu" else "used_percent")
        rows.append((name, f"{percent:.1f}%" if isinstance(percent, (int, float)) else "Unavailable", details(item)))
    for name, key in (("Disk", "disks"), ("Battery", "batteries"), ("Network", "network"), ("GPU", "gpu"), ("Process", "top_processes")):
        for item in data.get(key) or []:
            label = str(item.get("mount") or item.get("name") or name)
            if key == "disks":
                value = f"{size_label(item.get('used', 0))} / {size_label(item.get('total', 0))}"
            elif key == "batteries":
                value = f"{item.get('percent', '—')}% · {item.get('status', '')}"
            elif key == "network":
                rx, tx = item.get("rx_rate_mbs"), item.get("tx_rate_mbs")
                value = f"↓ {rx:.2f} / ↑ {tx:.2f} MiB/s" if rx is not None and tx is not None else "Waiting for next sample"
            else:
                value = str(item.get("usage", item.get("cpu", "—"))) + "%"
            rows.append((f"{name} · {label}", value, details(item)))
    for name, key in (("Hardware", "hardware"), ("Thermal", "thermal"), ("Disk I/O", "disk_io")):
        for label, value in (data.get(key) or {}).items():
            rows.append((f"{name} · {label.replace('_', ' ')}", formatted(label, value), ""))
    for label, value in (data.get("unavailable") or {}).items():
        rows.append((label, "Unavailable", str(value)))
    rows.append(("Uptime", str(data.get("uptime", "—")), str(data.get("collected_at", ""))))
    return rows
