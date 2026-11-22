"""Reviewed native macOS maintenance actions and conservative Touch ID setup."""
import asyncio
import base64
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import uuid


@dataclass(frozen=True)
class Action:
    key: str
    title: str
    description: str
    commands: tuple[tuple[str, ...], ...]
    administrator: bool = False


ACTIONS = (
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


def action_details(action):
    return action.title + "\n" + action.description + "\n" + ("Administrator authorization required.\n" if action.administrator else "") + "\n".join(shlex.join(argv) for argv in action.commands)


async def run_process(argv, output=None, timeout=180):
    """Bounded, cancellable output; argv is always passed without a shell."""
    process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "LC_ALL": "C"}, start_new_session=True)
    lines = []
    try:
        async def collect():
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
            if "(-128)" in result or "User canceled" in result:
                raise RuntimeError("Administrator authorization was canceled. No further actions were started.")
            raise RuntimeError(f"Command exited with status {code}: {result[-3000:]}")
        return result
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()


def administrator_argv(script):
    # Standard Additions displays the system authentication dialog. The app
    # never reads the password. Only internally constructed scripts reach here.
    quoted = script.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    # StandardAdditions.sdef: sysoexec = do shell script, badm = administrator
    # privileges, alen = altering line endings. Explicit event terms also compile
    # when a restricted host cannot load the scripting addition's dictionary.
    source = f'with timeout of 180 seconds\n«event sysoexec» "{quoted}" given «class badm»:true, «class alen»:false\nend timeout'
    return ("/usr/bin/osascript", "-e", source)


async def run_action(action, output=None):
    if action not in ACTIONS:
        raise ValueError("Unknown maintenance action")
    if any(not Path(argv[0]).is_file() for argv in action.commands):
        raise OSError("This action is unavailable on this macOS version")
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


def read_pam(directory=PAM_DIRECTORY):
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


def touchid_status(directory=PAM_DIRECTORY):
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


def plan_touchid(enabled, directory=PAM_DIRECTORY):
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


def touchid_script(plan):
    """A fixed-path, compare-before-write PAM edit with backup and atomic replacement."""
    target = str(PAM_DIRECTORY / "sudo_local")
    main = str(PAM_DIRECTORY / "sudo")
    digest = hashlib.sha256(plan.before).hexdigest() if plan.before is not None else None
    data = base64.b64encode(plan.after).decode("ascii")
    backup = target + ".system-steward-backup-" + uuid.uuid4().hex
    expected = f'[ "$(/usr/bin/shasum -a 256 {main} | /usr/bin/awk \'{{print $1}}\')" = {shlex.quote(plan.sudo_digest)} ]'
    if digest is None:
        expected += f' && [ ! -e {target} ]'
    else:
        expected += f' && [ -f {target} ] && [ "$(/usr/bin/shasum -a 256 {target} | /usr/bin/awk \'{{print $1}}\')" = {shlex.quote(digest)} ]'
    # The PAM directory is root-owned; no user-writable helper is run as root.
    return f'''set -eu
[ ! -L {PAM_DIRECTORY} ] && [ ! -L {target} ] && [ ! -L {main} ] || {{ echo 'Redirected PAM file; changes refused' >&2; exit 1; }}
/bin/mkdir {PAM_DIRECTORY}/.mac-steward-lock || {{ echo 'Another configuration edit is active' >&2; exit 1; }}
pam_tmp=''
trap '[ -z "$pam_tmp" ] || /bin/rm -f "$pam_tmp"; /bin/rmdir {PAM_DIRECTORY}/.mac-steward-lock' EXIT
{expected} || {{ echo 'Authentication settings changed since review; refresh before retrying' >&2; exit 1; }}
pam_tmp=$(/usr/bin/mktemp {PAM_DIRECTORY}/.system-steward.XXXXXX)
/usr/bin/printf '%s' {shlex.quote(data)} | /usr/bin/base64 -D > "$pam_tmp"
/usr/sbin/chown root:wheel "$pam_tmp"
/bin/chmod 444 "$pam_tmp"
{('/bin/cp -p ' + target + ' ' + backup + '\n/bin/chmod 600 ' + backup) if digest is not None else ':'}
{expected} || {{ echo 'Authentication settings changed during write; changes refused' >&2; exit 1; }}
/bin/mv -f "$pam_tmp" {target}
pam_tmp=''
echo 'Touch ID configuration updated.'
{('echo ' + shlex.quote('Backup: ' + backup)) if digest is not None else ':'}
'''


async def apply_touchid(plan, output=None):
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
