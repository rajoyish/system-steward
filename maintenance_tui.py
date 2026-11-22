"""Native Textual resource and maintenance screens (Windows and macOS)."""
import asyncio
from pathlib import Path
import subprocess
import threading

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, RichLog, Select, SelectionList, Static

from app_identity import BRAND
import native_maintenance as maintenance
from strings import TEXT
import system_actions
from system_status import SystemSampler
from system_steward import Entry, Scan, size_label


class MaintenanceConfirm(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "Cancel"), ("n", "cancel", "Cancel"), ("y", "confirm", "Confirm")]

    def __init__(self, title, details, confirm_label="Confirm action"):
        super().__init__()
        self.heading, self.details = title, details
        self.confirm_label = confirm_label
        self.decided = False

    def compose(self):
        with Vertical(classes="dialog", id="maintenance-confirm"):
            yield Label(self.heading, classes="dialog-title")
            with VerticalScroll():
                yield Static(Text(self.details))
            yield Static("Y confirms · Esc cancels · Enter activates the focused button", classes="confirmation-hint")
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="maintenance-cancel")
                yield Button(self.confirm_label, variant="error", id="maintenance-confirm-run")

    def on_mount(self):
        self.query_one("#maintenance-cancel", Button).focus()

    @on(Button.Pressed, "#maintenance-cancel")
    def action_cancel(self):
        if not self.decided:
            self.decided = True
            self.dismiss(False)

    @on(Button.Pressed, "#maintenance-confirm-run")
    def action_confirm(self):
        if not self.decided:
            self.decided = True
            self.dismiss(True)


class MaintenanceScreen(Screen):
    BINDINGS = [("escape", "back", "Back"), ("q", "back", "Back"),
                ("r", "refresh", "Refresh"), ("ctrl+a", "select_all", "Select all"),
                ("ctrl+d", "select_none", "Select none")]

    def __init__(self, home=None, root=None, timeout=120, initial_kind="status"):
        super().__init__()
        self.home = Path(home or Path.home()).resolve()
        self.start_root = root
        self.timeout = timeout
        self.kind = initial_kind
        self.groups = []
        self.scan = None
        self.warnings = []
        self.cancel_discovery = threading.Event()
        self.busy = False
        self.fetching = False
        self.preview_ready = False
        self.stop_requested = False
        self.last_status = None
        self.status_details = {}
        self.sampler = SystemSampler()
        self.actions = list(system_actions.ACTIONS)

    def compose(self):
        with Horizontal(classes="topbar"):
            yield Label(BRAND, classes="brand")
            yield Label("SYSTEM & MAINTENANCE", classes="top-context")
            yield Button("← Back", id="maintenance-back")
        with Vertical(id="maintenance-workspace"):
            with Horizontal(id="maintenance-toolbar"):
                yield Select([*maintenance.FEATURES[:1], ("Analyze Disk", "analyze"), *maintenance.FEATURES[1:]], value=self.kind, allow_blank=False, id="maintenance-feature")
                yield Button("↻ Refresh", id="maintenance-refresh")
            yield Static("", id="maintenance-summary")
            with Horizontal(id="maintenance-settings"):
                yield Select([(f"{n}s refresh", n) for n in (3, 5, 10, 30)], value=5, allow_blank=False, id="health-interval")
            yield Input(placeholder="Folder to analyze / scan for developer artifacts", value=str(self.start_root or self.home / "sites"), id="maintenance-path")
            yield Static("", id="maintenance-note")
            yield DataTable(id="health-table", cursor_type="row", zebra_stripes=True)
            yield SelectionList(id="maintenance-list")
            yield RichLog(id="maintenance-log", wrap=True, markup=False, max_lines=2000)
            with VerticalScroll(id="maintenance-detail-panel", classes="panel"):
                yield Static("", id="maintenance-detail")
            with Horizontal(id="maintenance-actions"):
                yield Button("Select all", id="maintenance-all")
                yield Button("Select none", id="maintenance-none")
                yield Button("Stop after item", id="maintenance-stop", disabled=True)
                yield Button("Review selection →", id="maintenance-review", variant="primary", disabled=True)
                yield Button("Enable Touch ID", id="touchid-enable")
                yield Button("Disable Touch ID", id="touchid-disable")
        yield Footer()

    def on_mount(self):
        self.query_one("#health-table", DataTable).add_columns("METRIC", "VALUE")
        self.timer = self.set_interval(5, self.refresh_health)
        self.set_class(self.app.size.width < 100, "narrow")
        self.set_class(self.app.size.height < 34, "short")
        self.action_refresh()

    def on_resize(self, event):
        self.set_class(event.size.width < 100, "narrow")
        self.set_class(event.size.height < 34, "short")

    def show(self, identifier, visible):
        self.query_one("#" + identifier).display = visible

    def configure(self):
        native = self.kind in {"clean", "purge", "installer", "uninstall", "optimize"}
        for identifier, visible in {
            "health-table": self.kind == "status", "health-interval": self.kind == "status",
            "maintenance-settings": self.kind == "status",
            "maintenance-list": native, "maintenance-all": native, "maintenance-none": native,
            "maintenance-log": self.kind == "touchid",
            "maintenance-path": self.kind in {"analyze", "purge"},
            "touchid-enable": self.kind == "touchid", "touchid-disable": self.kind == "touchid",
            "maintenance-review": self.kind not in {"status", "touchid"},
        }.items():
            self.show(identifier, visible)
        self.query_one("#maintenance-review", Button).label = {
            "analyze": "Explore folder →", "optimize": "Review selected tasks →"
        }.get(self.kind, "Review selection →")
        notes = {
            "status": "Live system health · select a metric for details. Refresh keeps the previous sample until a new one arrives.",
            "analyze": "Browse any folder by size using System Steward's native explorer.",
            "clean": TEXT.clean_note,
            "purge": "Scan the folder above. Only generated dependencies and build output are offered. Stop dev servers and builds before removal.",
            "installer": f"Downloaded {TEXT.installer_types} files in Downloads, Desktop and Documents. Installed applications are kept.",
            "uninstall": "Apps plus exact bundle-ID residuals. Quit the app first. Shared data and unmatched residuals are kept.",
            "optimize": "Choose individual maintenance tasks, inspect their effects, then review and confirm. " + TEXT.admin_prompt,
            "touchid": "Configure Touch ID in macOS’s sudo_local file. Changes preserve unrelated settings and use the standard administrator dialog.",
        }
        self.query_one("#maintenance-note", Static).update(notes[self.kind])
        self.query_one("#maintenance-detail", Static).update("")
        self.query_one("#maintenance-actions").refresh(layout=True)
        self.query_one("#maintenance-workspace").refresh(layout=True)

    @on(Select.Changed, "#maintenance-feature")
    def feature_changed(self, event):
        if event.value != Select.BLANK and event.value != self.kind and not self.busy:
            if event.value == "awake":
                self.query_one("#maintenance-feature", Select).value = self.kind
                self.app.open_system_tool("awake")
                return
            self.kind = event.value
            self.action_refresh()

    @on(Select.Changed, "#health-interval")
    def interval_changed(self, event):
        if hasattr(self, "timer") and event.value != Select.BLANK:
            self.timer.stop()
            self.timer = self.set_interval(event.value, self.refresh_health)

    def refresh_health(self):
        if self.kind == "status" and not self.fetching and not self.busy and self.app.screen is self:
            self.action_refresh()

    @on(Button.Pressed, "#maintenance-refresh")
    def action_refresh(self):
        if self.busy:
            return
        self.cancel_discovery.set()
        self.cancel_discovery = threading.Event()
        self.configure()
        self.preview_ready = False
        self.query_one("#maintenance-review", Button).disabled = True
        self.run_worker(self.load(self.kind, self.cancel_discovery), group="maintenance-load", exclusive=True)

    async def load(self, kind, cancel):
        self.fetching = True
        self.groups = []
        self.query_one("#maintenance-list", SelectionList).clear_options()
        self.query_one("#maintenance-summary", Static).update("◐ Loading…")
        log = self.query_one("#maintenance-log", RichLog)
        log.clear()
        try:
            if self.scan:
                old, self.scan = self.scan, None
                await asyncio.to_thread(old.close)
            if kind in {"clean", "purge", "installer", "uninstall"}:
                root = Path(self.query_one("#maintenance-path", Input).value).expanduser().absolute()
                if kind == "purge" and not root.is_dir():
                    raise ValueError("Choose an existing project folder, then refresh.")
                groups, warnings = await asyncio.to_thread(maintenance.discover, kind, self.home, root, cancel)
                if cancel.is_set():
                    return
                self.groups, self.warnings = groups, warnings
                entries = [Entry(str(path), str(path), kind) for group in groups for path in group.targets]
                scan = self.scan = Scan(entries, self.timeout)
                while not scan.finished.is_set():
                    done = sum(e.status not in {"queued", "scanning"} for e in entries)
                    self.query_one("#maintenance-summary", Static).update(f"◐ Measuring {done}/{len(entries)} locations…")
                    await asyncio.sleep(.15)
                self.measurements = {Path(e.path): e for e in entries}
                widget = self.query_one("#maintenance-list", SelectionList)
                widget.clear_options()
                for index, group in enumerate(groups):
                    total = sum(self.measurements[p].size for p in group.targets)
                    partial = any(self.measurements[p].status != "done" for p in group.targets)
                    widget.add_option((Text(f"{group.label}   {size_label(total)}{' * incomplete' if partial else ''}"), index, False))
                self.preview_ready = True
                self.query_one("#maintenance-summary", Static).update(f"{len(groups)} groups · {size_label(sum(e.size for e in entries))} measured · {len(warnings)} skipped · nothing selected")
                if warnings:
                    self.query_one("#maintenance-detail", Static).update(Text("Skipped paths:\n" + "\n".join(warnings)))
                widget.focus()
            elif kind == "status":
                data = await asyncio.to_thread(self.sampler.sample)
                if not isinstance(data, dict):
                    raise ValueError("Invalid native system sample")
                self.last_status = data
                table = self.query_one("#health-table", DataTable)
                selected = table.cursor_row
                table.clear()
                self.status_details = {}
                for index, (label, value, detail) in enumerate(maintenance.status_rows(data)):
                    table.add_row(Text(label), Text(value), key=str(index))
                    self.status_details[str(index)] = detail
                table.move_cursor(row=min(selected, max(0, table.row_count - 1)))
                warning = " · △ Partial sample: " + data["_warnings"] if data.get("_warnings") else ""
                score = data.get("health_score")
                label = f"{score}/100" if score is not None else "unavailable"
                self.query_one("#maintenance-summary", Static).update(Text(f"Resource estimate {label} · {data.get('collected_at', '')}" + warning))
            elif kind == "analyze":
                self.query_one("#maintenance-summary", Static).update("Choose a starting folder and open the size-sorted explorer.")
                self.preview_ready = True
            elif kind == "optimize":
                widget = self.query_one("#maintenance-list", SelectionList)
                for index, action in enumerate(self.actions):
                    available = system_actions.action_available(action)
                    label = action.title + (" · admin" if action.administrator else "")
                    widget.add_option((Text(label), index, False))
                    if not available:
                        widget.disable_option_at_index(index)
                self.query_one("#maintenance-summary", Static).update("Choose maintenance tasks · nothing selected")
                log.write("No changes have been applied. Select a task to inspect its exact commands and effect.")
                self.preview_ready = True
                widget.focus()
                if self.actions:
                    widget.highlighted = 0
                    self.query_one("#maintenance-detail", Static).update(Text(system_actions.action_details(self.actions[0])))
            elif kind == "touchid":
                status = await asyncio.to_thread(system_actions.touchid_status)
                log.write(Text(status["description"] + "\n\n" + status["details"]))
                self.query_one("#maintenance-summary", Static).update(status["description"])
                self.query_one("#touchid-enable", Button).disabled = not status["supported"] or status["enabled"]
                self.query_one("#touchid-disable", Button).disabled = not status["supported"] or not status["enabled"]
        except asyncio.CancelledError:
            cancel.set()
            raise
        except (OSError, ValueError, RuntimeError, asyncio.TimeoutError) as exc:
            message = str(exc) or "Operation timed out. Try refreshing."
            self.query_one("#maintenance-summary", Static).update(Text("△ " + message))
        finally:
            if not cancel.is_set():
                self.fetching = False
                self.update_selection()

    @on(SelectionList.SelectedChanged, "#maintenance-list")
    def update_selection(self):
        selected = self.query_one("#maintenance-list", SelectionList).selected
        ready = self.preview_ready and not self.fetching and not self.busy
        self.query_one("#maintenance-review", Button).disabled = not ready or (self.kind in {"clean", "purge", "installer", "uninstall", "optimize"} and not selected)
        if self.kind == "optimize":
            if selected:
                self.query_one("#maintenance-summary", Static).update(f"{len(selected)} maintenance tasks selected · review before applying")
            return
        if selected:
            groups = [self.groups[i] for i in selected]
            amount = sum(self.measurements[p].size for group in groups for p in group.targets)
            self.query_one("#maintenance-summary", Static).update(f"{len(selected)} selected · {size_label(amount)} measured · moves to {TEXT.trash}; {TEXT.empty_trash}")

    @on(SelectionList.SelectionHighlighted, "#maintenance-list")
    def highlight(self, event):
        if self.kind == "optimize":
            if event.selection.value < len(self.actions):
                self.query_one("#maintenance-detail", Static).update(Text(system_actions.action_details(self.actions[event.selection.value])))
            return
        if event.selection.value < len(self.groups):
            group = self.groups[event.selection.value]
            self.query_one("#maintenance-detail", Static).update(Text(group.label + "\n" + group.effect + "\n\n" + "\n".join(str(p) for p in group.targets) + ("\n\nSkipped:\n" + "\n".join(self.warnings) if self.warnings else "")))

    @on(DataTable.RowHighlighted, "#health-table")
    def health_highlight(self, event):
        self.query_one("#maintenance-detail", Static).update(Text(self.status_details.get(event.row_key.value, "")))

    @on(Button.Pressed, "#maintenance-all")
    def action_select_all(self):
        if self.preview_ready and not self.busy:
            self.query_one("#maintenance-list", SelectionList).select_all()

    @on(Button.Pressed, "#maintenance-none")
    def action_select_none(self):
        if not self.busy:
            self.query_one("#maintenance-list", SelectionList).deselect_all()

    @on(Button.Pressed, "#maintenance-review")
    def review(self):
        if not self.preview_ready or self.fetching or self.busy:
            return
        if self.kind == "analyze":
            root = Path(self.query_one("#maintenance-path", Input).value).expanduser().absolute()
            if not root.is_dir():
                self.notify("Choose an existing folder.", severity="error")
                return
            self.app.root, self.app.group, self.app.stack = root, "All", []
            self.app.by_size = True
            self.app.search_text = ""
            self.app.pop_screen()
            self.app.query_one("#search", Input).value = ""
            self.app.refresh_storage()
            return
        if self.kind == "optimize":
            chosen = tuple(self.actions[i] for i in self.query_one("#maintenance-list", SelectionList).selected)
            if chosen:
                details = "Only these tasks will run. " + TEXT.admin_prompt + "\n\n" + "\n\n".join(system_actions.action_details(action) for action in chosen)
                self.app.push_screen(MaintenanceConfirm("Review selected maintenance tasks", details, "Run selected tasks"),
                    lambda approved: self.run_worker(self.execute_actions(chosen), group="maintenance-action") if approved else None)
            return
        chosen = [self.groups[i] for i in self.query_one("#maintenance-list", SelectionList).selected]
        if not chosen:
            return
        details = f"Only these targets will move to {TEXT.trash}. Quit related apps and stop builds first. {TEXT.restore_hint} {TEXT.empty_trash.capitalize()}.\n\n" + "\n\n".join(g.label + "\n" + g.effect + "\n" + "\n".join(str(p) for p in g.targets) for g in chosen)
        self.app.push_screen(MaintenanceConfirm(f"Review {len(chosen)} selected groups", details, f"Move to {TEXT.trash_title}"), lambda approved: self.run_worker(self.trash_selected(chosen), group="maintenance-action") if approved else None)

    def confirm_touchid(self, enabled):
        if self.fetching or self.busy:
            return
        try:
            plan = system_actions.plan_touchid(enabled)
        except (OSError, ValueError) as exc:
            self.notify(str(exc), severity="error")
            return
        details = plan.description + "\n\nProposed /etc/pam.d/sudo_local contents:\n\n" + plan.after.decode("utf-8")
        self.app.push_screen(MaintenanceConfirm("Enable Touch ID for sudo" if enabled else "Disable Touch ID for sudo", details, "Enable Touch ID" if enabled else "Disable Touch ID"),
            lambda approved: self.run_worker(self.execute_touchid(plan), group="maintenance-action") if approved else None)

    @on(Button.Pressed, "#touchid-enable")
    def enable_touchid(self):
        self.confirm_touchid(True)

    @on(Button.Pressed, "#touchid-disable")
    def disable_touchid(self):
        self.confirm_touchid(False)

    def set_busy(self, value):
        self.busy = value
        for identifier in ("maintenance-feature", "maintenance-refresh", "maintenance-path", "maintenance-list", "maintenance-all", "maintenance-none", "touchid-enable", "touchid-disable"):
            self.query_one("#" + identifier).disabled = value
        self.query_one("#maintenance-stop", Button).disabled = not value
        self.update_selection()

    async def execute_actions(self, actions):
        self.set_busy(True)
        self.preview_ready = False
        self.stop_requested = False
        self.show("maintenance-list", False)
        self.show("maintenance-log", True)
        log = self.query_one("#maintenance-log", RichLog)
        log.clear()
        completed = 0
        try:
            for action in actions:
                if self.stop_requested:
                    break
                self.query_one("#maintenance-summary", Static).update(Text("◐ " + action.title))
                log.write(Text(system_actions.action_details(action)))
                await system_actions.run_action(action, lambda line: log.write(Text(line)))
                completed += 1
                log.write("✓ Task completed.\n")
            self.query_one("#maintenance-summary", Static).update(f"{completed}/{len(actions)} tasks completed. " + ("Stopped before remaining tasks." if self.stop_requested else "Refresh to review or run another task."))
        except (OSError, ValueError, RuntimeError, asyncio.TimeoutError) as exc:
            message = str(exc) or "Command timed out"
            log.write(Text("△ " + message))
            self.query_one("#maintenance-summary", Static).update(Text(f"△ {completed}/{len(actions)} completed. The failed task may have applied some steps. Remaining tasks were not started. " + message))
        finally:
            self.set_busy(False)

    async def execute_touchid(self, plan):
        self.set_busy(True)
        log = self.query_one("#maintenance-log", RichLog)
        log.clear()
        self.query_one("#maintenance-summary", Static).update("◐ Waiting for administrator authorization…")
        try:
            await system_actions.apply_touchid(plan, lambda line: log.write(Text(line)))
            self.query_one("#maintenance-summary", Static).update("✓ Touch ID configuration verified. Refresh to check status.")
        except (OSError, ValueError, RuntimeError, asyncio.TimeoutError) as exc:
            message = str(exc) or "Authorization timed out"
            log.write(Text("△ " + message))
            self.query_one("#maintenance-summary", Static).update(Text("△ " + message))
        finally:
            self.set_busy(False)
            try:
                status = await asyncio.to_thread(system_actions.touchid_status)
                self.query_one("#touchid-enable", Button).disabled = not status["supported"] or status["enabled"]
                self.query_one("#touchid-disable", Button).disabled = not status["supported"] or not status["enabled"]
            except (OSError, ValueError):
                self.query_one("#touchid-enable", Button).disabled = True
                self.query_one("#touchid-disable", Button).disabled = True

    async def trash_selected(self, chosen):
        self.set_busy(True)
        self.stop_requested = False
        self.preview_ready = False
        results = []
        try:
            # Do not move running apps or their data. Fail closed if process inspection fails.
            apps = [p for g in chosen for p in g.targets if p.suffix == ".app"]
            if apps:
                output = await asyncio.to_thread(subprocess.check_output, ["/bin/ps", "-axo", "command="], text=True)
                if any(str(p) + "/" in output for p in apps):
                    raise RuntimeError("A selected app is running. Quit it and refresh before trying again.")
            for group in chosen:
                if self.stop_requested:
                    results.append("Stopped. Remaining groups were kept.")
                    break
                self.query_one("#maintenance-summary", Static).update(Text(f"Moving to {TEXT.trash_title}: " + group.label))
                try:
                    moved = []
                    if any(p.suffix == ".app" for p in group.targets):
                        output = await asyncio.to_thread(subprocess.check_output, ["/bin/ps", "-axo", "command="], text=True)
                        if any(str(p) + "/" in output for p in group.targets if p.suffix == ".app"):
                            raise ValueError("App relaunched after confirmation; this group was kept.")
                    await asyncio.to_thread(maintenance.trash_group, group, self.home, moved)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    results.append(f"△ {group.label}: {exc}")
                finally:
                    results.extend(f"✓ {src}\n  → {dest}" for src, dest in moved)
            self.query_one("#maintenance-summary", Static).update(f"Finished · review results below. Refresh before another cleanup. {TEXT.empty_trash.capitalize()}.")
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            results.append(str(exc))
            self.query_one("#maintenance-summary", Static).update("△ Cleanup blocked · see details")
        finally:
            self.query_one("#maintenance-detail", Static).update(Text("\n\n".join(results)))
            self.set_busy(False)

    @on(Button.Pressed, "#maintenance-stop")
    def stop(self):
        if self.kind in {"clean", "purge", "installer", "uninstall", "optimize"}:
            self.stop_requested = True
            self.query_one("#maintenance-stop", Button).disabled = True
        else:
            self.notify("Maintenance command is running; wait for completion or its timeout.")

    @on(Button.Pressed, "#maintenance-back")
    async def action_back(self):
        if self.busy:
            self.notify("Wait for the current action, or stop after the current item.", severity="warning")
            return
        self.cancel_discovery.set()
        self.workers.cancel_group(self, "maintenance-load")
        if self.scan:
            await asyncio.to_thread(self.scan.close)
        self.app.pop_screen()
        self.app.refresh_storage()

    async def on_unmount(self):
        self.cancel_discovery.set()
        if self.scan:
            await asyncio.to_thread(self.scan.close)
