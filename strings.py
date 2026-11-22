"""User-facing wording that differs between Windows and macOS.

Screens import these instead of hard-coding one platform's terms (Trash vs Recycle Bin, ⌘Q vs
closing the window, Activity Monitor vs Task Manager, ...). ``for_platform`` lets tests render
either variant on any host.
"""
from dataclasses import dataclass
import sys


@dataclass(frozen=True)
class Wording:
    os_name: str
    trash: str                # inline noun: "move to {trash}"
    trash_title: str          # button/heading noun
    empty_trash: str          # how space is reclaimed
    restore_hint: str
    quit_app: str             # how to quit an app completely
    process_viewer: str
    admin_prompt: str
    keep_awake_engine: str
    keep_awake_intro: str
    keep_awake_unavailable: str
    command_example: str
    installer_types: str
    clean_note: str
    sampled_from: str


WINDOWS = Wording(
    os_name="Windows",
    trash="the Recycle Bin",
    trash_title="Recycle Bin",
    empty_trash="empty the Recycle Bin separately to reclaim disk space",
    restore_hint="Restore items from the Recycle Bin if needed.",
    quit_app="close every window and exit it from the notification area (system tray)",
    process_viewer="Task Manager",
    admin_prompt="Windows shows a User Account Control prompt for tasks that need administrator rights.",
    keep_awake_engine="Windows SetThreadExecutionState",
    keep_awake_intro=("Temporarily prevent sleep with Windows SetThreadExecutionState. Power & sleep settings stay "
                      "unchanged. Foreground sessions stop when you quit; background sessions keep running."),
    keep_awake_unavailable="Keep Awake is unavailable on this system.",
    command_example='e.g. robocopy "C:\\source folder" "D:\\backup folder" /E',
    installer_types=".exe, .msi and .iso",
    clean_note=("Temporary files, crash dumps and developer caches only. Quit related apps first. Selected folders "
                "move to the Recycle Bin; empty it separately to reclaim disk space."),
    sampled_from="CPU, memory, network and power are sampled directly from Windows APIs.",
)

MACOS = Wording(
    os_name="macOS",
    trash="Trash",
    trash_title="Trash",
    empty_trash="empty Trash separately to reclaim disk space",
    restore_hint="Restore items from Trash if needed.",
    quit_app="quit it with ⌘Q",
    process_viewer="Activity Monitor",
    admin_prompt="macOS will request administrator authorization where required.",
    keep_awake_engine="macOS caffeinate",
    keep_awake_intro=("Temporarily prevent sleep with macOS caffeinate. System Settings stay unchanged. "
                      "Foreground sessions stop when you quit; background sessions keep running."),
    keep_awake_unavailable="Keep Awake is available on macOS only.",
    command_example='e.g. /usr/bin/ditto "source folder" "backup folder"',
    installer_types=".dmg, .pkg and .iso",
    clean_note=("User caches and logs only. Quit related apps first. Selected folders move to Trash; "
                "empty Trash separately to reclaim disk space."),
    sampled_from="CPU, memory, network and power are sampled directly from macOS.",
)


def for_platform(platform: str | None = None) -> Wording:
    return WINDOWS if (platform or sys.platform) == "win32" else MACOS


TEXT = for_platform()
