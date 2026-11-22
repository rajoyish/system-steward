"""Reviewed native maintenance actions (Windows and macOS) and conservative macOS Touch ID setup."""
import asyncio
import base64
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import sys
from typing import Any
import uuid

Output = Callable[[str], None]


@dataclass(frozen=True)
class Action:
    key: str
    title: str
    description: str
    commands: tuple[tuple[str, ...], ...]
    administrator: bool = False


MAC_ACTIONS = (
    Action("dns", "Refresh DNS and directory caches", "Flush directory-service caches and ask mDNSResponder to reload. Use for name-resolution problems; lookups may briefly slow while caches rebuild.",
           (("/usr/bin/dscacheutil", "-flushcache"), ("/usr/bin/killall", "-HUP", "mDNSResponder")), True),
    Action("quicklook", "Refresh Quick Look cache", "Reset cached file previews and reload Quick Look generators. Previews rebuild when next used.",
           (("/usr/bin/qlmanage", "-r", "cache"), ("/usr/bin/qlmanage", "-r"))),
    Action("launchservices", "Refresh application registration", "Compact Launch Services records and rescan system, local and user applications. Use to repair stale application registrations; this can take a few minutes.",
           (("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister", "-gc"),
            ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister", "-f", "-r", "-apps", "system,local,user"))),
    Action("spotlight", "Rebuild startup-disk Spotlight index", "Erase and rebuild the startup disk's search index. Search results may be incomplete and CPU/disk activity may increase while indexing runs. Completion here means the rebuild was requested, not that indexing finished.",
           (("/usr/bin/mdutil", "-E", "/"),), True),
)

# Windows equivalents. These are what actually runs on Windows, so the review screen shows them.
WINDOWS_ACTIONS = (
    Action("dns", "Flush DNS resolver cache", "Clear cached name lookups. Use for name-resolution problems; lookups may briefly slow while the cache refills.",
           (("ipconfig.exe", "/flushdns"),)),
    Action("iconcache", "Refresh icon cache", "Ask Windows to rebuild its icon cache so stale or wrong icons refresh. Explorer may redraw briefly.",
           (("ie4uinit.exe", "-show"),)),
    Action("store", "Repair Microsoft Store", "Run wsreset -i, which clears the Store cache and reinstalls the Store app if it is missing or broken. The Store may open when it finishes.",
           (("wsreset.exe", "-i"),)),
    Action("search", "Restart Windows Search indexer", "Restart the Windows Search service (WSearch). Search results may be incomplete for a moment while it resumes. Needs administrator approval.",
           (("pwsh.exe", "-NoProfile", "-NonInteractive", "-Command", "Restart-Service WSearch -Force"),), True),
)


def actions_for(platform: str | None = None) -> tuple[Action, ...]:
    return WINDOWS_ACTIONS if (platform or sys.platform) == "win32" else MAC_ACTIONS


ACTIONS = actions_for()


def action_details(action: Action) -> str:
    return action.title + "\n" + action.description + "\n" + ("Administrator authorization required.\n" if action.administrator else "") + "\n".join(shlex.join(argv) for argv in action.commands)


async def run_process(argv: Sequence[str], output: Output | None = None, timeout: float = 180) -> str:
    """Bounded, cancellable output; argv is always passed without a shell."""
    if sys.platform == "win32":
        first = str(argv[0])
        if not first.lower().endswith((".exe", ".cmd", ".bat", ".com", ".ps1")) and os.path.isfile(first):
            sh = shutil.which("sh")
            if sh:
                argv = [sh, *argv]
    process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "LC_ALL": "C"},
        start_new_session=True if sys.platform != "win32" else False)
    lines = []
    try:
        async def collect() -> int:
            while True:
                chunk = await process.stdout.readline()
                if not chunk:
                    break
                line = chunk.decode(errors="replace").rstrip()
                lines.append(line)
                del lines[:-2000]
                if output:
                    output(line)
            return await process.wait()
        code = await asyncio.wait_for(collect(), timeout)
        result = "\n".join(lines)
        if code:
            if "(-128)" in result or "User canceled" in result or "canceled by the user" in result:
                raise RuntimeError("Administrator authorization was canceled. No further actions were started.")
            raise RuntimeError(f"Command exited with status {code}: {result[-3000:]}")
        return result
    finally:
        if process.returncode is None:
            if hasattr(os, "killpg"):
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                try:
                    process.terminate()
                except OSError:
                    pass
            try:
                await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError:
                if hasattr(os, "killpg"):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    try:
                        process.kill()
                    except OSError:
                        pass
                await process.wait()
        # A timeout cancels the reader mid-stream, and a grandchild (e.g. under `sh`) can keep the pipe
        # open, so EOF may never arrive. Close the transport explicitly so its pipe handles are
        # released (otherwise: ResourceWarning "unclosed transport" and a closed-pipe error at GC).
        transport = getattr(process, "_transport", None)
        if transport is not None:
            transport.close()


def administrator_argv(script: str) -> tuple[str, ...]:
    # Standard Additions displays the system authentication dialog. The app
    # never reads the password. Only internally constructed scripts reach here.
    quoted = script.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    # StandardAdditions.sdef: sysoexec = do shell script, badm = administrator
    # privileges, alen = altering line endings. Explicit event terms also compile
    # when a restricted host cannot load the scripting addition's dictionary.
    source = f'with timeout of 180 seconds\n«event sysoexec» "{quoted}" given «class badm»:true, «class alen»:false\nend timeout'
    return ("/usr/bin/osascript", "-e", source)


def powershell_literal(text: object) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def windows_elevated_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Run fixed ``argv`` through a UAC prompt and propagate its exit code.

    Only internally defined commands reach here; each argument is a quoted PowerShell literal.
    """
    arguments = ", ".join(powershell_literal(part) for part in argv[1:]) or "@()"
    script = (f"$p = Start-Process -FilePath {powershell_literal(argv[0])} -ArgumentList {arguments} "
              "-Verb RunAs -Wait -PassThru -WindowStyle Hidden; exit $p.ExitCode")
    return ("pwsh.exe", "-NoProfile", "-NonInteractive", "-Command", script)


def action_available(action: Action, platform: str | None = None) -> bool:
    """Whether the action's programs exist here (absolute macOS paths, or Windows programs on PATH)."""
    if (platform or sys.platform) == "win32":
        return all(shutil.which(argv[0]) for argv in action.commands)
    return all(Path(argv[0]).is_file() for argv in action.commands)


async def run_action(action: Action, output: Output | None = None, platform: str | None = None) -> None:
    """Run one reviewed action. ``platform`` ("win32"/"darwin") defaults to this host."""
    platform = platform or sys.platform
    if action not in actions_for(platform):
        raise ValueError("Unknown maintenance action")
    if platform == "darwin" and any(not Path(argv[0]).is_file() for argv in action.commands):
        raise OSError("This action is unavailable on this macOS version")
    if platform == "win32":
        for argv in action.commands:
            await run_process(list(windows_elevated_argv(argv) if action.administrator else argv), output, timeout=180)
        return
    if action.administrator:
        # Fixed executable paths and arguments from ACTIONS, quoted independently.
        script = "set -eu\n" + "\n".join(shlex.join(argv) for argv in action.commands)
        await run_process(administrator_argv(script), output, timeout=180)
    else:
        for argv in action.commands:
            await run_process(argv, output, timeout=180)


PAM_DIRECTORY = Path("/private/etc/pam.d")
PAM_ACTIVE = re.compile(r"^\s*auth\s+(?:\[[^\]]+\]|\S+)\s+(?:\S*/)?pam_tid\.so(?:\.\d+)?(?:\s|$)")
PAM_INCLUDE = re.compile(r"^\s*auth\s+include\s+sudo_local(?:\s|$)", re.M)


@dataclass(frozen=True)
class TouchIDPlan:
    enabled: bool
    before: bytes | None
    after: bytes
    sudo_digest: str
    description: str


def read_pam(directory: str | os.PathLike[str] = PAM_DIRECTORY) -> tuple[bytes | None, ...]:
    directory = Path(directory)
    # Canonical /private/etc avoids /etc's standard macOS symlink.
    if directory.is_symlink() or directory.resolve() != directory:
        raise ValueError("Authentication configuration directory is redirected")
    result = []
    for name in ("sudo", "sudo_local"):
        path = directory / name
        if path.is_symlink():
            raise ValueError(f"Refusing redirected authentication file: {name}")
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"Authentication configuration is not a regular file: {name}")
            result.append(path.read_bytes())
        except FileNotFoundError:
            if name == "sudo":
                raise
            result.append(None)
    return tuple(result)


def touchid_status(directory: str | os.PathLike[str] = PAM_DIRECTORY) -> dict[str, Any]:
    sudo, local = read_pam(directory)
    main_text = sudo.decode("utf-8")
    local_text = (local or b"").decode("utf-8")
    included = bool(PAM_INCLUDE.search(main_text))
    legacy = any(PAM_ACTIVE.match(line) for line in main_text.splitlines())
    active = any(PAM_ACTIVE.match(line) for line in local_text.splitlines())
    enabled = legacy or (included and active)
    return {"enabled": enabled, "legacy": legacy, "supported": included and not legacy,
            "description": "Touch ID is enabled for sudo." if enabled else "Touch ID is disabled for sudo.",
            "details": "\n".join(("Configuration: /etc/pam.d/sudo_local", "Local include: " + ("present" if included else "missing"),
                "Legacy entry in system sudo file: " + ("present; automatic changes disabled" if legacy else "none"),
                "A compatible Touch ID sensor and enrolled fingerprint are required to authenticate. Configuration alone does not verify hardware availability."))}


def plan_touchid(enabled: bool, directory: str | os.PathLike[str] = PAM_DIRECTORY) -> TouchIDPlan:
    sudo, local = read_pam(directory)
    status = touchid_status(directory)
    if not status["supported"]:
        raise ValueError("Automatic Touch ID changes require macOS's sudo_local include and no legacy entry in /etc/pam.d/sudo. The system sudo file will not be edited.")
    text = (local or b"# Local sudo authentication settings\n").decode("utf-8")
    if enabled:
        if not any(PAM_ACTIVE.match(line) for line in text.splitlines()):
            text = "auth       sufficient     pam_tid.so\n" + text
    else:
        text = "".join("# Disabled by System Steward: " + line if PAM_ACTIVE.match(line) else line for line in text.splitlines(keepends=True))
    after = text.encode("utf-8")
    if not enabled and local is None:
        after = b""
    description = ("Enable" if enabled else "Disable") + " Touch ID for sudo in /etc/pam.d/sudo_local. Preserve all unrelated settings and create a backup of an existing file. macOS will request administrator authorization."
    return TouchIDPlan(enabled, local, after, hashlib.sha256(sudo).hexdigest(), description)


def touchid_script(plan: TouchIDPlan) -> str:
    """A fixed-path, compare-before-write PAM edit with backup and atomic replacement."""
    pam_dir = str(PAM_DIRECTORY).replace("\\", "/")
    target = f"{pam_dir}/sudo_local"
    main = f"{pam_dir}/sudo"
    digest = hashlib.sha256(plan.before).hexdigest() if plan.before is not None else None
    data = base64.b64encode(plan.after).decode("ascii")
    backup = target + ".system-steward-backup-" + uuid.uuid4().hex
    shasum_calc = f"(/usr/bin/shasum -a 256 {main} 2>/dev/null || sha256sum {main}) | (/usr/bin/awk '{{print $1}}' 2>/dev/null || awk '{{print $1}}')"
    target_calc = f"(/usr/bin/shasum -a 256 {target} 2>/dev/null || sha256sum {target}) | (/usr/bin/awk '{{print $1}}' 2>/dev/null || awk '{{print $1}}')"
    expected = f'[ "$({shasum_calc})" = {shlex.quote(plan.sudo_digest)} ]'
    if digest is None:
        expected += f' && [ ! -e {target} ]'
    else:
        expected += f' && [ -f {target} ] && [ "$({target_calc})" = {shlex.quote(digest)} ]'
    # Built outside the f-string: a backslash inside an f-string expression is a SyntaxError
    # before Python 3.12, and this project supports 3.10+.
    backup_step = (f"/bin/cp -p {target} {backup}\n/bin/chmod 600 {backup}") if digest is not None else ":"
    backup_note = ("echo " + shlex.quote("Backup: " + backup)) if digest is not None else ":"
    # The PAM directory is root-owned; no user-writable helper is run as root.
    return f'''set -eu
[ ! -L {pam_dir} ] && [ ! -L {target} ] && [ ! -L {main} ] || {{ echo 'Redirected PAM file; changes refused' >&2; exit 1; }}
/bin/mkdir {pam_dir}/.mac-steward-lock || {{ echo 'Another configuration edit is active' >&2; exit 1; }}
pam_tmp=''
trap '[ -z "$pam_tmp" ] || /bin/rm -f "$pam_tmp"; /bin/rmdir {pam_dir}/.mac-steward-lock' EXIT
{expected} || {{ echo 'Authentication settings changed since review; refresh before retrying' >&2; exit 1; }}
pam_tmp=$(/usr/bin/mktemp {pam_dir}/.system-steward.XXXXXX 2>/dev/null || mktemp {pam_dir}/.system-steward.XXXXXX)
/usr/bin/printf '%s' {shlex.quote(data)} | (/usr/bin/base64 -D 2>/dev/null || base64 -d) > "$pam_tmp"
/usr/sbin/chown root:wheel "$pam_tmp"
/bin/chmod 444 "$pam_tmp"
{backup_step}
{expected} || {{ echo 'Authentication settings changed during write; changes refused' >&2; exit 1; }}
/bin/mv -f "$pam_tmp" {target}
pam_tmp=''
echo 'Touch ID configuration updated.'
{backup_note}
'''


async def apply_touchid(plan: TouchIDPlan, output: Output | None = None) -> None:
    if not isinstance(plan, TouchIDPlan):
        raise ValueError("Invalid Touch ID plan")
    sudo, local = read_pam()
    if local != plan.before or hashlib.sha256(sudo).hexdigest() != plan.sudo_digest:
        raise ValueError("Authentication settings changed since review; refresh before retrying")
    if local == plan.after or (local is None and not plan.after):
        if output:
            output("Configuration already matches the requested state.")
        return
    await run_process(administrator_argv(touchid_script(plan)), output, timeout=180)
    status = touchid_status()
    if status["enabled"] != plan.enabled:
        raise RuntimeError("Configuration was written but verification did not match the requested state")
    if output:
        output(status["description"])
