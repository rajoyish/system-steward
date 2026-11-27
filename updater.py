"""Update check and self-update through the package manager that installed System Steward.

How each install channel is recognised (from the install folder, never from PATH):
  Scoop     the install root holds Scoop's ``scoop-manifest.json``
  winget    the install root holds the Inno Setup uninstaller ``unins000.exe``
  Homebrew  the code lives under ``…/Cellar/system-steward/``
Source checkouts and the curl/irm install scripts are not managed here and never check.

The newest version is read from what the channel itself serves (the Scoop manifest, the Homebrew
formula, the winget-pkgs manifest folders), so a fresh tag is only offered once that channel can
actually install it. Results are cached for a day in the state folder; set
``SYSTEM_STEWARD_NO_UPDATE_CHECK=1`` to turn the check off.

A running app cannot replace itself: Scoop refuses while its python.exe runs, and the winget
installer overwrites the same files. So on Windows the app writes an update script, exits with
``UPDATE_EXIT``, and the ``system-steward.cmd`` launcher runs that script once Python has exited;
the script updates, then starts the new version. Homebrew allows upgrading in place, so on macOS
the app runs ``brew upgrade`` itself after the interface closes and re-executes the new version.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from app_identity import VERSION
from awake_state import state_dir

REPO = "rajoyish/system-steward"
WINGET_ID = "RajeshBudhathoki.SystemSteward"
SCOOP_MANIFEST_URL = f"https://raw.githubusercontent.com/{REPO}/master/bucket/system-steward.json"
BREW_FORMULA_URL = f"https://raw.githubusercontent.com/{REPO}/master/Formula/system-steward.rb"
WINGET_VERSIONS_URL = ("https://api.github.com/repos/microsoft/winget-pkgs/contents/manifests/r/"
                       "RajeshBudhathoki/SystemSteward")
CHECK_INTERVAL = 24 * 3600
# A failed or declined attempt is not offered again for this long, so a broken update never loops.
RETRY_INTERVAL = 24 * 3600
NETWORK_TIMEOUT = 4.0
# The Windows launcher runs the update script when the app exits with this code (EX_TEMPFAIL).
UPDATE_EXIT = 75
STATE_NAME = "update_check.json"
SCRIPT_NAME = "system-steward-update.cmd"


@dataclass(frozen=True)
class Channel:
    name: str          # "scoop" | "winget" | "brew"
    label: str         # shown to people: "Scoop", "winget", "Homebrew"
    root: Path         # install root (holds the launcher on Windows)


@dataclass(frozen=True)
class Update:
    channel: Channel
    current: str
    latest: str


def parse_version(text: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def newer(latest: str, current: str = VERSION) -> bool:
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


# ---------------------------------------------------------------- channel detection

def install_root() -> Path:
    """The folder that holds ``app/`` in a packaged install (unresolved, so Scoop's ``current`` stays)."""
    return Path(os.path.abspath(__file__)).parent.parent


def detect_channel(root: Path | None = None) -> Channel | None:
    root = root if root is not None else install_root()
    if (root / "scoop-manifest.json").is_file():
        return Channel("scoop", "Scoop", root)
    if (root / "unins000.exe").is_file() and (root / "system-steward.cmd").is_file():
        return Channel("winget", "winget", root)
    if "/cellar/system-steward/" in Path(root).resolve().as_posix().lower() + "/":
        return Channel("brew", "Homebrew", root)
    return None


def checks_enabled() -> bool:
    return os.environ.get("SYSTEM_STEWARD_NO_UPDATE_CHECK", "").strip().lower() not in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- latest version per channel

def fetch(url: str, timeout: float = NETWORK_TIMEOUT) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": f"system-steward/{VERSION}",
                                                   "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URLs
        return response.read(1_000_000).decode("utf-8")


def latest_for(channel: Channel, get: Callable[[str], str] = fetch) -> str | None:
    if channel.name == "scoop":
        version = json.loads(get(SCOOP_MANIFEST_URL)).get("version")
        return version if isinstance(version, str) and parse_version(version) else None
    if channel.name == "brew":
        match = re.search(r"/releases/download/v(\d+(?:\.\d+)*)/", get(BREW_FORMULA_URL))
        return match.group(1) if match else None
    if channel.name == "winget":
        names = [item.get("name", "") for item in json.loads(get(WINGET_VERSIONS_URL)) if item.get("type") == "dir"]
        versions = [name for name in names if parse_version(name)]
        return max(versions, key=parse_version, default=None)
    return None


# ---------------------------------------------------------------- cached state

def state_path() -> Path:
    return state_dir() / STATE_NAME


def load_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(data: dict) -> None:
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def skip_version(version: str) -> None:
    data = load_state()
    data["skipped"] = version
    save_state(data)


def record_attempt(version: str) -> None:
    data = load_state()
    data["attempted"] = {"version": version, "at": time.time()}
    save_state(data)


def check(channel: Channel | None = None, *, force: bool = False, now: float | None = None,
          get: Callable[[str], str] = fetch) -> Update | None:
    """Newer version this channel can install, or None. Network errors are silent (None).

    Without ``force`` the result is cached for CHECK_INTERVAL, and skipped or recently attempted
    versions are not offered.
    """
    channel = channel if channel is not None else detect_channel()
    if channel is None:
        return None
    now = time.time() if now is None else now
    data = load_state()
    cached = data.get("latest", {})
    fresh = (isinstance(cached, dict) and cached.get("channel") == channel.name
             and now - float(cached.get("checked_at", 0)) < CHECK_INTERVAL)
    if fresh and not force:
        latest = cached.get("version")
    else:
        try:
            latest = latest_for(channel, get)
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        data["latest"] = {"channel": channel.name, "version": latest, "checked_at": now}
        save_state(data)
    if not isinstance(latest, str) or not newer(latest):
        return None
    if not force:
        if data.get("skipped") == latest:
            return None
        attempted = data.get("attempted") or {}
        if attempted.get("version") == latest and now - float(attempted.get("at", 0)) < RETRY_INTERVAL:
            return None
    return Update(channel, VERSION, latest)


# ---------------------------------------------------------------- performing the update

def windows_commands(channel: Channel) -> list[list[str]]:
    if channel.name == "scoop":
        scoop = shutil.which("scoop.cmd") or shutil.which("scoop") or "scoop"
        return [[scoop, "update"], [scoop, "update", "system-steward"]]
    if channel.name == "winget":
        winget = shutil.which("winget") or "winget"
        return [[winget, "upgrade", "--id", WINGET_ID, "--exact", "--source", "winget"]]
    raise ValueError(f"{channel.label} updates are not run on Windows")


def batch_quote(arg: str) -> str:
    """Quote one argument for a .cmd line. Arguments holding a double quote cannot be passed safely."""
    if '"' in arg or "\n" in arg or "\r" in arg:
        raise ValueError("argument contains a quote or newline")
    return '"' + arg.replace("%", "%%") + '"'


def windows_script(update: Update, relaunch_args: list[str]) -> str:
    channel = update.channel
    launcher = channel.root / "system-steward.cmd"
    safe_args = []
    for arg in relaunch_args:
        try:
            safe_args.append(batch_quote(arg))
        except ValueError:
            continue
    lines = ["@echo off", "echo.",
             f"echo Updating System Steward {update.current} to {update.latest} with {channel.label}...", "echo."]
    lines += ["call " + " ".join(batch_quote(part) for part in command) for command in windows_commands(channel)]
    lines.append("if errorlevel 1 (echo. & echo Update did not finish. Starting the installed version. & echo.)")
    lines += ["echo.", f"call {batch_quote(str(launcher))} {' '.join(safe_args)}".rstrip()]
    return "\r\n".join(lines) + "\r\n"


def prepare_windows(update: Update, relaunch_args: list[str]) -> Path:
    """Write the script the launcher runs after Python exits; returns its path."""
    path = Path(tempfile.gettempdir()) / SCRIPT_NAME
    # Bytes, not text mode: the script already has CRLF endings and text mode would double the CR.
    path.write_bytes(windows_script(update, relaunch_args).encode("oem" if sys.platform == "win32" else "utf-8",
                                                                  errors="replace"))
    return path


def stop_keep_awake_for_update() -> bool:
    """Scoop and the winget installer cannot replace the bundled python while Keep Awake runs it."""
    from keep_awake import AwakeSession
    session = AwakeSession()
    if session.poll():
        session.stop()
        return True
    return False


def apply(update: Update, relaunch_args: list[str]) -> int:
    """Run the update for ``update``; returns the exit code main() should return."""
    record_attempt(update.latest)
    if update.channel.name in ("scoop", "winget"):
        if sys.platform != "win32":
            raise RuntimeError(f"{update.channel.label} updates run on Windows only")
        if stop_keep_awake_for_update():
            print("Keep Awake stopped so the update can replace its files.")
        prepare_windows(update, relaunch_args)
        return UPDATE_EXIT
    if update.channel.name == "brew":
        brew = shutil.which("brew")
        if not brew:
            print("Homebrew not found on PATH; run: brew upgrade system-steward", file=sys.stderr)
            return 1
        print(f"Updating System Steward {update.current} to {update.latest} with Homebrew…\n")
        result = subprocess.run([brew, "upgrade", "rajoyish/system-steward/system-steward"], check=False)
        if result.returncode != 0:
            print("\nUpdate did not finish. Starting the installed version.\n", file=sys.stderr)
        target = shutil.which("system-steward")
        if target:
            os.execv(target, [target, *relaunch_args])
        return result.returncode
    raise ValueError(f"unknown channel {update.channel.name}")


def manual_command(channel: Channel) -> str:
    return {"scoop": "scoop update system-steward",
            "winget": f"winget upgrade --id {WINGET_ID}",
            "brew": "brew upgrade system-steward"}[channel.name]
