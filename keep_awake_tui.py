"""Keep Awake controls; the application owns the session across screen changes."""
import asyncio
from pathlib import Path
import shlex
import subprocess
import sys

from rich.text import Text
from textual import on
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Footer, Input, Label, Select, Static

from app_identity import BRAND
from keep_awake import available, plan_session
from maintenance_tui import MaintenanceConfirm
from native_maintenance import ANSI
from system_steward import clean


class KeepAwakeScreen(Screen):
    BINDINGS = [("escape", "back", "Back"), ("q", "back", "Back")]

    def __init__(self, session, root=None):
        super().__init__()
        self.session = session
        self.root = root or Path.cwd()
        self.stopping = False

    def compose(self):
        with Horizontal(classes="topbar"):
            yield Label(BRAND, classes="brand")
            context_label = "KEEP AWAKE · Windows" if sys.platform == "win32" else "KEEP AWAKE · macOS"
            yield Label(context_label, classes="top-context")
            yield Button("← Back", id="awake-back")
        with VerticalScroll(id="awake-workspace"):
            yield Label("Keep Awake", classes="page-title")
            copy_text = ("Temporarily prevent sleep with Windows SetThreadExecutionState. Power & sleep settings stay unchanged. Sessions continue while you browse the app and stop when you quit."
                if sys.platform == "win32" else
                "Temporarily prevent sleep with macOS caffeinate. System Settings stay unchanged. Sessions continue while you browse the app and stop when you quit.")
            yield Static(copy_text, classes="awake-copy")
            yield Static("", id="awake-status")
            yield Label("Prevent", classes="eyebrow")
            yield Checkbox("Idle system sleep (-i)", value=True, id="awake-idle")
            yield Checkbox("Display sleep (-d)", id="awake-display")
            yield Checkbox("Disk idle sleep (-m)", id="awake-disk")
            yield Checkbox("Run in background (persists after closing terminal/app)", value=True, id="awake-background")
            yield Label("Session ends", classes="eyebrow")
            yield Select([("When stopped / timer ends", "manual"), ("When an existing PID exits", "process"),
                ("When a command finishes", "command")], value="manual", allow_blank=False, id="awake-mode")
            yield Label("Timer in seconds · blank means no timer", id="awake-duration-label")
            yield Input(placeholder="e.g. 3600 for one hour", id="awake-duration")
            yield Label("Existing process ID", id="awake-pid-label")
            yield Input(placeholder="e.g. 12345", id="awake-pid")
            yield Label("Command and arguments", id="awake-command-label")
            yield Input(placeholder='e.g. /usr/bin/ditto "source folder" "backup folder"', id="awake-command")
            yield Label("Working folder for the command", id="awake-cwd-label")
            yield Input(value=str(self.root), id="awake-cwd")
            yield Static("", id="awake-mode-note", classes="awake-copy")
            with Horizontal(id="awake-actions"):
                yield Button("Review & start →", id="awake-start", variant="primary")
                yield Button("Stop session", id="awake-stop", disabled=True)
            yield Static("", id="awake-preview", classes="awake-copy")
            yield Static("", id="awake-output", markup=False)
        yield Footer()

    def on_mount(self):
        plan = self.session.plan
        if plan:
            self.query_one("#awake-mode", Select).value = plan.mode
            for flag, name in (("i", "idle"), ("d", "display"), ("m", "disk")):
                self.query_one(f"#awake-{name}", Checkbox).value = flag in plan.argv[1]
            if hasattr(plan, "background") and self.query("#awake-background"):
                self.query_one("#awake-background", Checkbox).value = plan.background
            self.query_one("#awake-duration", Input).value = str(plan.seconds) if plan.seconds else ""
            self.query_one("#awake-cwd", Input).value = str(plan.cwd)
            if plan.mode == "process":
                self.query_one("#awake-pid", Input).value = plan.argv[-1]
            elif plan.mode == "command":
                self.query_one("#awake-command", Input).value = shlex.join(plan.argv[2:])
        self.configure_mode()
        self.refresh_session()
        self.set_interval(1, self.refresh_session)

    @on(Select.Changed, "#awake-mode")
    def configure_mode(self):
        mode = self.query_one("#awake-mode", Select).value
        for name, visible in {"duration": mode != "command", "pid": mode == "process",
                "command": mode == "command", "cwd": mode == "command", "background": mode != "command"}.items():
            if self.query(f"#awake-{name}"):
                self.query_one(f"#awake-{name}").display = visible
            if self.query(f"#awake-{name}-label"):
                self.query_one(f"#awake-{name}-label").display = visible
        note = {
            "manual": "Use Stop session to end early. Background sessions persist when terminal closes (~2-4 MB memory) and turn off on shutdown.",
            "process": "Stops when this PID exits or the timer expires. Stopping this session leaves the watched process running.",
            "command": "Runs in the background without interactive input. Quotes group arguments; shell operators and expansions are not interpreted. Timer is unavailable. Stopping the session or quitting the app also stops the command."
        }.get(mode, "")
        self.query_one("#awake-mode-note", Static).update(note)

    def refresh_session(self):
        running = self.session.running
        self.query_one("#awake-status", Static).update(Text(self.session.status() if available() else "Keep Awake is available on macOS only."))
        self.query_one("#awake-start", Button).disabled = running or self.stopping or not available()
        self.query_one("#awake-stop", Button).disabled = not running or self.stopping
        for widget in self.query("Checkbox, Input, Select"):
            widget.disabled = running or self.stopping
        if self.session.plan:
            self.query_one("#awake-preview", Static).update(Text("Session: " + self.session.plan.command + "\nFolder: " + str(self.session.plan.cwd)))
        output = ANSI.sub("", self.session.output_tail())
        self.query_one("#awake-output", Static).update(Text("\n".join(clean(line) for line in output.splitlines())))

    @on(Button.Pressed, "#awake-start")
    def review_start(self):
        if self.session.running or self.stopping:
            return
        try:
            flags = [flag for flag, name in (("i", "idle"), ("d", "display"), ("m", "disk"))
                if self.query_one(f"#awake-{name}", Checkbox).value]
            mode = self.query_one("#awake-mode", Select).value
            background = self.query_one("#awake-background", Checkbox).value if mode != "command" and self.query("#awake-background") else False
            plan = plan_session(flags, mode,
                duration=self.query_one("#awake-duration", Input).value if mode != "command" else "",
                pid=self.query_one("#awake-pid", Input).value,
                command=self.query_one("#awake-command", Input).value,
                cwd=self.query_one("#awake-cwd", Input).value if mode == "command" else None,
                background=background)
        except (ValueError, OSError) as exc:
            self.notify(str(exc), severity="error")
            return
        details = "Command:\n" + plan.command + "\n\nWorking folder:\n" + str(plan.cwd)
        if background:
            details += "\n\nRuns in background (~2-4 MB RAM). Remains active even if you close the terminal or quit System Steward, and turns off when your computer turns off."
        else:
            details += "\n\nNo power settings are changed. Leaving this screen keeps the session running. Quitting the app stops it."
        if mode == "command":
            details += "\n\nThis runs the entered command with your user permissions and no interactive input. Stop session and app exit also terminate that command and its process group."
        self.app.push_screen(MaintenanceConfirm("Start Keep Awake?", details, "Start session"),
            lambda approved: self.start_session(plan) if approved else None)

    def start_session(self, plan):
        try:
            self.session.start(plan)
            if getattr(plan, "background", False):
                self.notify("Keep Awake active in background. Terminal and app can be closed safely.", title="Keep Awake")
        except (ValueError, OSError, RuntimeError) as exc:
            self.notify(str(exc), severity="error")
        self.refresh_session()

    @on(Button.Pressed, "#awake-stop")
    def review_stop(self):
        if not self.session.running or self.stopping:
            return
        if self.session.plan.mode == "command":
            self.app.push_screen(MaintenanceConfirm("Stop command and Keep Awake?",
                "The running command and its process group will be terminated. Save work before stopping.", "Stop command"),
                lambda approved: self.run_worker(self.stop_session()) if approved else None)
        else:
            self.run_worker(self.stop_session())

    async def stop_session(self):
        self.stopping = True
        self.refresh_session()
        try:
            await asyncio.to_thread(self.session.stop)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            self.notify(str(exc), severity="error")
        finally:
            self.stopping = False
            self.refresh_session()

    @on(Button.Pressed, "#awake-back")
    def action_back(self):
        if self.stopping:
            self.notify("Wait for the session to stop.")
            return
        self.app.pop_screen()
