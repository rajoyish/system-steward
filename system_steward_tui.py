"""Textual interface: inspect storage, select cleanup groups, review, clear."""
import asyncio
import datetime
import json
from pathlib import Path
import shutil

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import Button, DataTable, Footer, Input, Label, ProgressBar, Select, SelectionList, Static, Tab, Tabs

import adobe_cleanup
import chrome_cleanup
from app_identity import APP_NAME, AUTHOR, BRAND, VERSION
from dashboard import DashboardCard, METRICS, TOOLS, metric_values
from system_status import SystemSampler
from maintenance_tui import MaintenanceConfirm, MaintenanceScreen
from keep_awake import AwakeSession
from keep_awake_tui import KeepAwakeScreen
from cleanup_selection import cleanup_groups, selection_plan
from system_steward import GROUPS, GROUP_ICONS, Entry, Scan, audit_entries, children, clean, entry_icon, pretty_path, report, size_label


def display_path(path):
    return Text(pretty_path(path))


class CleanupList(SelectionList):
    BINDINGS = [Binding("space", "select", "Toggle group")]


class InfoDialog(ModalScreen):
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, title, content):
        super().__init__()
        self.heading, self.content = title, content

    def compose(self):
        with Vertical(id="info-dialog", classes="dialog"):
            yield Label(self.heading, classes="dialog-title")
            with VerticalScroll():
                yield Static(Text(self.content))
            yield Button("Close", id="close-info", variant="primary")

    @on(Button.Pressed, "#close-info")
    def action_close(self):
        self.dismiss()


class ConfirmDialog(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "Cancel"), ("n", "cancel", "Cancel"), ("y", "confirm", "Confirm")]

    def __init__(self, kind, groups, amount, checker):
        super().__init__()
        self.kind, self.groups, self.amount, self.checker = kind, groups, amount, checker
        self.checking = False

    def compose(self):
        with Vertical(id="confirm-dialog", classes="dialog"):
            yield Label("REVIEW & CONFIRM", classes="eyebrow")
            yield Label(f"Clear {len(self.groups)} selected groups?", classes="dialog-title")
            yield Static(f"{size_label(self.amount)} measured · {self.kind} · permanent deletion", id="confirm-summary")
            with VerticalScroll(id="confirm-paths"):
                for group in self.groups:
                    yield Static(Text(f"• {group.label}\n  {pretty_path(group.path)}\n  {group.effect}\n"))
            yield Static("There is no undo. Close Adobe apps and Creative Cloud before continuing." if self.kind == "Adobe"
                         else "There is no undo. Close Chrome before continuing. Website-data groups sign you out.", id="confirm-warning")
            yield Static("Only the selected groups will be cleared.", id="confirm-status")
            yield Static("Y confirms · Esc cancels · Enter activates the focused button", classes="confirmation-hint")
            with Horizontal(classes="dialog-actions"):
                yield Button("Go back", id="cancel-delete")
                yield Button("Permanently clear", variant="error", id="confirm-delete")

    def on_mount(self):
        self.query_one("#cancel-delete", Button).focus()
        self.update_confirmation()

    def update_confirmation(self):
        small = self.app.size.width < 64 or self.app.size.height < 24
        self.query_one("#confirm-delete", Button).disabled = self.checking or small
        self.query_one("#cancel-delete", Button).disabled = self.checking

    def on_resize(self):
        self.set_class(self.app.size.height < 32, "short")
        if self.is_mounted:
            self.update_confirmation()

    @on(Button.Pressed, "#cancel-delete")
    def action_cancel(self):
        if not self.checking:
            self.dismiss(False)

    @on(Button.Pressed, "#confirm-delete")
    @work(exclusive=True)
    async def action_confirm(self):
        if self.checking or self.app.size.width < 64 or self.app.size.height < 24:
            return
        self.checking = True
        self.update_confirmation()
        status = self.query_one("#confirm-status", Static)
        status.update("Checking that applications are closed…")
        try:
            running = await asyncio.to_thread(self.checker)
            if running:
                status.update(f"{self.kind} apps or helpers are still running. Quit them with ⌘Q, then retry.")
            else:
                self.dismiss(True)
                return
        except OSError as exc:
            status.update(Text(str(exc)))
        finally:
            self.checking = False
            if self.is_mounted:
                self.update_confirmation()


class CleanupScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back"), Binding("ctrl+a", "select_all", "Select all"),
                Binding("ctrl+d", "select_none", "Select none"), Binding("g", "guide", "Guide"),
                Binding("p", "paths", "Exact paths"),
                Binding("q", "back", "Back")]

    def __init__(self, kind, home=None, timeout=120):
        super().__init__()
        self.kind, self.home, self.timeout = kind, home, timeout
        self.plan = self.scan = self.job = None
        self.groups = []
        self.scope = "cache"
        self.populated = False
        self.reviewing = False
        self.completed = False
        self.measurements = {}

    def compose(self):
        with Horizontal(classes="topbar"):
            yield Label(BRAND, classes="brand")
            yield Label(f"CLEANUP / {self.kind.upper()}", classes="top-context")
            yield Button("← Back", id="cleanup-back")
        with Vertical(id="cleanup-workspace"):
            icon = GROUP_ICONS["Browsers" if self.kind == "Chrome" else "Creative"]
            yield Label(f"{icon}  {self.kind} cleanup", classes="page-title")
            yield Label("01  Choose files    →    02  Review selection    →    03  Clear & verify", id="steps")
            with Horizontal(id="cleanup-toolbar"):
                if self.kind == "Chrome":
                    yield Select([("Browser caches only", "cache"), ("Website data + caches", "sites")],
                                 value="cache", allow_blank=False, id="cleanup-scope")
                else:
                    yield Label("Approved cache contents · parent folders stay", id="adobe-scope")
                    yield Button("Purge guide", id="purge-guide")
                yield Button("Select all", id="select-all", disabled=True)
                yield Button("Select none", id="select-none", disabled=True)
            with Horizontal(id="cleanup-panes"):
                with Vertical(id="cleanup-list-panel", classes="panel"):
                    yield Label("CHOOSE WHAT TO CLEAR", classes="eyebrow")
                    yield Static("Finding approved locations…", id="cleanup-state")
                    yield CleanupList(id="cleanup-list", disabled=True)
                    yield ProgressBar(total=None, show_eta=False, id="cleanup-progress")
                with VerticalScroll(id="cleanup-details", classes="panel"):
                    yield Label("SELECTION DETAILS", classes="eyebrow")
                    yield Static("Select a group to see its path and effect.", id="group-detail")
                    yield Label("ALWAYS KEPT", classes="eyebrow")
                    yield Static("Bookmarks\nBrowsing history\nSaved passwords\nInstalled extensions" if self.kind == "Chrome"
                                 else "Cache parent folders\nLightroom catalogs\nPresets & plugins\nScripts & workspaces\nLibrary Preferences", id="preserved")
                    yield Button("View skipped paths", id="skipped", disabled=True)
                    yield Button("Inspect selected paths", id="exact-paths")
            yield Static("Nothing selected. Space toggles a group; click or use ↑↓ to move.", id="selection-summary")
            yield Static("", id="cleanup-result")
            with Horizontal(id="cleanup-actions"):
                yield Static("Nothing is deleted until you review and confirm.", id="cleanup-note")
                yield Button("Stop after current item", id="stop-cleanup", disabled=True)
                yield Button("Review selection →", variant="primary", id="review", disabled=True)
        yield Footer()

    def on_mount(self):
        self.run_worker(self.discover(), exclusive=True, group="discovery")
        self.set_class(self.app.size.height < 34, "short")
        self.set_interval(.2, self.tick)

    def on_resize(self, event):
        self.set_class(event.size.height < 34, "short")

    async def discover(self):
        self.query_one("#review", Button).disabled = True
        self.query_one("#select-all", Button).disabled = True
        self.query_one("#select-none", Button).disabled = True
        self.query_one("#cleanup-list", SelectionList).disabled = True
        self.populated = False
        old_scan, self.scan, self.plan = self.scan, None, None
        self.query_one("#cleanup-state", Static).update("Finding approved locations…")
        if old_scan:
            await asyncio.to_thread(old_scan.close)
        try:
            builder = chrome_cleanup.build_plan if self.kind == "Chrome" else adobe_cleanup.build_plan
            plan = await asyncio.to_thread(builder, self.home)
            self.plan = plan
            self.groups = cleanup_groups(self.kind, plan, self.scope)
            targets = tuple(path for group in self.groups for path in group.targets)
            self.scan = Scan([Entry(str(path), str(path), self.kind) for path in targets], self.timeout)
            self.query_one("#skipped", Button).disabled = not plan.skipped
            self.query_one("#cleanup-progress", ProgressBar).update(total=max(1, len(targets)), progress=0)
            self.measurements = {}
            self.populate()
        except OSError as exc:
            self.plan = None
            self.query_one("#cleanup-state", Static).update(Text(f"Discovery failed: {exc}"))
            self.query_one("#cleanup-progress", ProgressBar).update(total=1, progress=0)

    def populate(self):
        widget = self.query_one("#cleanup-list", SelectionList)
        selected = set(widget.selected) if self.populated else set()
        widget.clear_options()
        for index, group in enumerate(self.groups):
            amount = sum(self.measurements.get(path, 0) for path in group.targets)
            label = Text(group.label, style="bold")
            label.append(f"   {size_label(amount)}", style="#71e5bb")
            if self.job and self.completed:
                cleared = sum(path in self.job.removed for path in group.targets)
                label.append(f"   {'✓ Cleared' if cleared == len(group.targets) else '◐ Partial' if cleared else 'Kept'}",
                             style="#71e5bb" if cleared else "#8395a5")
            widget.add_option((label, index, index in selected))
        self.populated = True
        if self.groups:
            widget.highlighted = 0
            self.show_group(0)
        self.update_selection()

    def tick(self):
        if not self.is_mounted or self.plan is None or self.scan is None or not list(self.query("#cleanup-state")):
            return
        if self.job:
            if self.completed:
                return
            count = len(self.job.removed) + len(self.job.errors)
            self.query_one("#cleanup-progress", ProgressBar).update(total=max(1, len(self.job.plan.targets)), progress=min(count, len(self.job.plan.targets)))
            self.query_one("#cleanup-state", Static).update(Text("Clearing: " + pretty_path(self.job.current)))
            if self.job.done.is_set() and not self.completed:
                self.completed = True
                self.show_result()
            return
        done = sum(entry.status not in ("queued", "scanning") for entry in self.scan.entries)
        ready = self.scan.finished.is_set()
        self.query_one("#cleanup-progress", ProgressBar).update(total=max(1, len(self.scan.entries)), progress=done if self.scan.entries else 1)
        self.query_one("#cleanup-state", Static).update(
            f"{len(self.groups)} groups ready · Space toggles selection" if ready and self.groups
            else "No eligible cache contents found." if ready else f"Measuring {done} / {len(self.scan.entries)} locations…")
        if ready and self.query_one("#cleanup-list", SelectionList).disabled:
            self.measurements = {Path(entry.path): entry.size for entry in self.scan.entries}
            self.populate()
            self.query_one("#cleanup-list", SelectionList).disabled = not self.groups
            for identifier in ("select-all", "select-none"):
                self.query_one(f"#{identifier}", Button).disabled = not self.groups
            self.query_one("#cleanup-list", SelectionList).focus()
            incomplete = sum(entry.status != "done" for entry in self.scan.entries)
            if incomplete:
                self.query_one("#cleanup-note", Static).update(f"{incomplete} sizes are incomplete; the estimate may undercount.")

    @on(Select.Changed, "#cleanup-scope")
    def scope_changed(self, event):
        if event.value != Select.BLANK and event.value != self.scope and not self.job:
            self.scope = event.value
            self.run_worker(self.discover(), exclusive=True, group="discovery")

    @on(SelectionList.SelectedChanged, "#cleanup-list")
    def update_selection(self):
        selected = set(self.query_one("#cleanup-list", SelectionList).selected)
        amount = sum(self.measurements.get(path, 0) for index, group in enumerate(self.groups) if index in selected for path in group.targets)
        targets = sum(len(group.targets) for index, group in enumerate(self.groups) if index in selected)
        self.query_one("#selection-summary", Static).update(Text(f"{len(selected)} groups selected  ·  {targets} locations  ·  {size_label(amount)} measured", style="bold"))
        ready = self.scan is not None and self.scan.finished.is_set()
        self.query_one("#review", Button).disabled = False if self.completed else not selected or not ready or self.job is not None or self.reviewing

    @on(SelectionList.SelectionHighlighted, "#cleanup-list")
    def highlight(self, event):
        self.show_group(event.selection.value)

    def show_group(self, index):
        if not 0 <= index < len(self.groups):
            return
        group = self.groups[index]
        content = f"{group.label}\n\n{pretty_path(group.path)}\n\n{len(group.targets)} locations\n\n{group.effect}"
        if len(group.targets) <= 8:
            content += "\n\n" + "\n".join("• " + pretty_path(path) for path in group.targets)
        self.query_one("#group-detail", Static).update(Text(content))

    @on(Button.Pressed, "#select-all")
    def action_select_all(self):
        if self.scan and self.scan.finished.is_set() and not self.job:
            self.query_one("#cleanup-list", SelectionList).select_all()

    @on(Button.Pressed, "#select-none")
    def action_select_none(self):
        if not self.job:
            self.query_one("#cleanup-list", SelectionList).deselect_all()

    @on(Button.Pressed, "#skipped")
    def show_skipped(self):
        if self.plan:
            self.app.push_screen(InfoDialog("Protected & skipped", "\n\n".join(pretty_path(path) for path in self.plan.skipped)))

    @on(Button.Pressed, "#purge-guide")
    def action_guide(self):
        if self.kind == "Adobe":
            self.app.push_screen(InfoDialog("Adobe · in-app purge guide", "\n".join(adobe_cleanup.PURGE_GUIDE)))

    @on(Button.Pressed, "#exact-paths")
    def action_paths(self):
        widget = self.query_one("#cleanup-list", SelectionList)
        selected = set(widget.selected)
        if not selected and widget.highlighted is not None:
            selected = {widget.highlighted}
        groups = [group for index, group in enumerate(self.groups) if index in selected]
        content = "\n\n".join(group.label + "\n" + "\n".join(pretty_path(path) for path in group.targets) for group in groups)
        self.app.push_screen(InfoDialog("Exact cleanup paths", content or "Select a cleanup group to inspect its paths."))

    @on(Button.Pressed, "#review")
    def review(self):
        if self.reviewing or self.job or not self.scan or not self.scan.finished.is_set():
            return
        selected = set(self.query_one("#cleanup-list", SelectionList).selected)
        if not selected:
            return
        self.reviewing = True
        self.update_selection()
        chosen = [group for index, group in enumerate(self.groups) if index in selected]
        restricted = selection_plan(self.plan, self.groups, selected)
        amount = sum(self.measurements.get(path, 0) for path in restricted.targets)
        checker = chrome_cleanup.chrome_running if self.kind == "Chrome" else adobe_cleanup.adobe_running

        def confirmed(approved):
            self.reviewing = False
            if approved:
                self.run_worker(self.begin_cleanup(restricted), group="delete", exclusive=True)
            else:
                self.update_selection()

        self.app.push_screen(ConfirmDialog(self.kind, chosen, amount, checker), confirmed)

    async def begin_cleanup(self, restricted):
        # Freeze selection before awaiting so the reviewed plan cannot change.
        self.reviewing = True
        for identifier in ("review", "select-all", "select-none"):
            self.query_one(f"#{identifier}", Button).disabled = True
        self.query_one("#cleanup-list", SelectionList).disabled = True
        if self.kind == "Chrome":
            self.query_one("#cleanup-scope", Select).disabled = True
        self.query_one("#steps", Label).update("01  Selected    →    02  Confirmed    →    03  Clearing selected contents")
        await self.app.pause_scan()
        await asyncio.to_thread(self.scan.close)
        job_type = chrome_cleanup.CleanupJob if self.kind == "Chrome" else adobe_cleanup.AdobeCleanupJob
        self.job = job_type(restricted)
        self.query_one("#stop-cleanup", Button).disabled = False
        self.query_one("#cleanup-note", Static).update("Keep the applications closed until cleanup finishes.")

    @on(Button.Pressed, "#stop-cleanup")
    def stop(self):
        if self.job and not self.job.done.is_set():
            self.job.cancel.set()
            self.query_one("#cleanup-note", Static).update("Stopping after the current location. Remaining items will stay.")
            self.query_one("#stop-cleanup", Button).disabled = True

    def show_result(self):
        self.query_one("#steps", Label).update("01  Selected    →    02  Confirmed    →    03  Complete")
        amount = sum(self.measurements.get(path, 0) for path in self.job.removed)
        summary = f"{len(self.job.removed)} locations cleared · {size_label(amount)} measured contents removed · {len(self.job.errors)} errors"
        self.query_one("#cleanup-state", Static).update(summary)
        content = "\n".join(self.job.errors[:4]) if self.job.errors else "Selected contents cleared. Unselected groups were kept."
        if len(self.job.errors) > 4:
            content += "\nSee the full results for all errors."
        self.query_one("#cleanup-result", Static).update(Text(content))
        self.query_one("#cleanup-result").display = True
        self.query_one("#cleanup-note", Static).update("Measured contents may differ from the change in available disk space.")
        self.query_one("#stop-cleanup", Button).disabled = True
        review = self.query_one("#review", Button)
        review.label = "View full results"
        review.disabled = False
        self.populate()
        review.disabled = False
        self.query_one("#cleanup-back", Button).label = "← Return to storage"

    @on(Button.Pressed, "#review")
    def result_details(self):
        if self.completed:
            content = "CLEARED\n" + "\n".join(pretty_path(path) for path in self.job.removed)
            content += "\n\nERRORS\n" + ("\n".join(self.job.errors) or "None")
            self.app.push_screen(InfoDialog("Cleanup results", content))

    @on(Button.Pressed, "#cleanup-back")
    async def action_back(self):
        if self.job and not self.job.done.is_set():
            self.notify("Stop cleanup or wait for it to finish before leaving.", severity="warning")
            return
        if self.scan:
            await asyncio.to_thread(self.scan.close)
        self.app.pop_screen()
        if self.job:
            self.app.refresh_storage()

    async def on_unmount(self):
        if self.scan:
            await asyncio.to_thread(self.scan.close)
        if self.job and not self.job.done.is_set():
            self.job.cancel.set()
            await asyncio.to_thread(self.job.thread.join)


class SystemStewardApp(App):
    TITLE = APP_NAME
    SUB_TITLE = f"by {AUTHOR}"
    CSS_PATH = "system_steward.tcss"
    BINDINGS = [Binding("q", "quit", "Quit"), Binding("ctrl+c", "quit", "Quit", show=False),
                Binding("/", "search", "Search"), Binding("r", "rescan", "Rescan"),
                Binding("s", "sort", "Sort"), Binding("backspace", "back", "Back"),
                Binding("c", "chrome", "Chrome"), Binding("a", "adobe", "Adobe"),
                Binding("m", "maintenance", "System tools"), Binding("k", "keep_awake", "Keep awake"), Binding("d", "dashboard", "Dashboard"),
                Binding("e", "export", "Export"), Binding("?", "help", "Help")]

    def __init__(self, root=None, timeout=120, home=None):
        super().__init__()
        self.root, self.timeout, self.home = root, timeout, home
        self.scan = None
        self.stack = []
        self.group = "All"
        self.search_text = ""
        self.by_size = True
        self.visible_entries = {}
        self.snapshot = None
        self.active_path = None
        self.show_footprint = True
        self.dashboard_expanded = None
        self.dashboard_manual = False
        self.dashboard_sampling = False
        self.dashboard_sampler = SystemSampler()
        self.dashboard_sample = None
        self.awake = AwakeSession()
        self.register_theme(Theme(name="system-steward", primary="#71e5bb", secondary="#96a6e8", accent="#96a6e8",
                                  foreground="#dde7ef", background="#0c1218", surface="#121c25", panel="#17232e",
                                  success="#71e5bb", warning="#edbf75", error="#f18087", dark=True))
        self.theme = "system-steward"

    def compose(self) -> ComposeResult:
        with Horizontal(classes="topbar"):
            with Vertical(id="app-identity"):
                yield Label(BRAND, classes="brand")
                credit = Label(f"by {AUTHOR}", id="version")
                credit.tooltip = f"{APP_NAME} {VERSION}"
                yield credit
            yield Label("YOUR SYSTEM, WITH ROOM TO BREATHE", classes="top-context")
            yield Button("System dashboard ▾", id="dashboard-toggle")
        with VerticalScroll(id="dashboard", can_focus=False):
            with Horizontal(id="metrics"):
                with Vertical(classes="metric"):
                    yield Label("VOLUME USED", classes="eyebrow")
                    yield Static("—", id="disk-used", classes="metric-value")
                    yield ProgressBar(total=100, show_percentage=False, show_eta=False, id="disk-bar")
                    yield Static("Space occupied on this volume", classes="metric-caption")
                with Vertical(classes="metric"):
                    yield Label("AVAILABLE", classes="eyebrow")
                    yield Static("—", id="disk-free", classes="metric-value positive")
                    yield Static("Free space on this volume", classes="metric-caption")
                with Vertical(classes="metric"):
                    yield Label("MEASURED LOCATIONS", classes="eyebrow")
                    yield Static("—", id="measured", classes="metric-value")
                    yield Static("Discovering your storage…", id="scan-status", classes="metric-caption")
                    yield Static("Total of the scanned locations", classes="metric-caption")
            with Vertical(id="dashboard-extras"):
                with Horizontal(id="system-metrics"):
                    for key, title, description, tooltip in METRICS:
                        card = DashboardCard(title, description, id=f"metric-{key}", classes="system-metric", tooltip=tooltip)
                        card.set_value("Reading…")
                        yield card
                with Horizontal(id="dashboard-tools-heading"):
                    yield Label("SYSTEM TOOLS", classes="eyebrow")
                    yield Static("Live readings every 5s · tools open previews before changes.", id="dashboard-health-status")
                with Vertical(id="dashboard-tools"):
                    for offset in (0, 3):
                        with Horizontal(classes="tool-row"):
                            for key, title, description in TOOLS[offset:offset + 3]:
                                yield DashboardCard(title, description, id=f"tool-{key}", classes="feature-card")
        with Horizontal(id="workspace"):
            with VerticalScroll(id="sidebar"):
                yield Label("QUICK ACCESS", classes="eyebrow")
                yield Button("◷  Keep awake", id="keep-awake")
                yield Label("CLEANUP & MAINTENANCE", classes="eyebrow")
                yield Button(f"{GROUP_ICONS['Browsers']}  Chrome cleanup", id="chrome")
                yield Button(f"{GROUP_ICONS['Creative']}  Adobe caches", id="adobe")
                yield Button("◇  System tools", id="maintenance")
                yield Static("Scan first.\nChoose deliberately.\nKeep what matters.", id="sidebar-note")
            with Vertical(id="main-panel", classes="panel"):
                with Horizontal(id="table-toolbar"):
                    yield Label("Overview", id="scope-title")
                    yield Input(placeholder="Search locations…  /", id="search")
                yield Static("Selected locations, not a full disk inventory", id="scope-path")
                with Horizontal(id="explore-navigation"):
                    yield Label("EXPLORE", classes="eyebrow")
                    yield Tabs(*(Tab(f"{GROUP_ICONS[group]}  {group if group != 'All' else 'Overview'}", id=group)
                                 for group in GROUPS), active="All", id="navigation")
                with Horizontal(id="inspector-panes"):
                    yield DataTable(id="files", cursor_type="row", zebra_stripes=True, show_row_labels=False)
                    with VerticalScroll(id="details"):
                        yield Label("INSPECT", classes="eyebrow")
                        yield Static("Select a location", id="detail-name")
                        yield Static("—", id="detail-size", classes="metric-value")
                        yield Button("Open folder →", id="explore", disabled=True)
                        yield Static("", id="detail-path")
                        yield Static("", id="detail-status")
                        yield Static("Directory sizes use allocated disk blocks. Partial results can undercount.", id="detail-hint")
                yield Static("↑↓ Select a location · Enter explores a folder", id="selected-path")
                with Horizontal(id="table-actions"):
                    yield Button("← Back", id="back")
                    yield Button("↻ Rescan", id="rescan")
                    yield Button("↓ Size", id="sort")
                    yield Button("↥ Export", id="export")
        yield Static("Enlarge the terminal to at least 64 × 22.", id="too-small")
        yield Footer()

    def on_mount(self):
        self.configure_columns()
        table = self.query_one("#files", DataTable)
        self.refresh_storage()
        self.set_interval(.25, self.tick)
        table.focus()
        self.resize_layout(self.size.width, self.size.height)
        self.set_interval(5, self.refresh_dashboard)
        self.set_interval(1, self.refresh_awake_indicator)
        self.refresh_dashboard()

    def configure_columns(self):
        table = self.query_one("#files", DataTable)
        table.clear(columns=True)
        table.add_column("LOCATION", key="name", width=28)
        table.add_column("ALLOCATED", key="size", width=12)
        if self.show_footprint:
            table.add_column("FOOTPRINT", key="share", width=10)
        table.add_column("STATUS", key="status", width=12)

    def on_resize(self, event):
        self.resize_layout(event.size.width, event.size.height)

    def resize_layout(self, width, height):
        if not self.dashboard_manual:
            self.dashboard_expanded = width >= 110 and height >= 44
        self.set_class(bool(self.dashboard_expanded), "dashboard-expanded")
        self.set_class(height < 36, "short-dashboard")
        self.set_class(width < 140, "dashboard-compact")
        if self.is_mounted:
            self.query_one("#dashboard-toggle", Button).label = "System dashboard ▴" if self.dashboard_expanded else "System dashboard ▾"
        self.set_class(width < 125, "compact")
        self.set_class(width < 100, "narrow")
        self.set_class(width < 64 or height < 22, "tiny")
        if self.is_mounted and self.show_footprint != (width >= 85):
            self.show_footprint = width >= 85
            self.configure_columns()
            self.snapshot = None
            self.tick()

    def refresh_storage(self):
        if self.root and not self.root.is_dir():
            self.root, self.group, self.stack = None, "All", []
        navigation = self.query_one("#navigation", Tabs)
        with navigation.prevent(Tabs.TabActivated):
            navigation.active = self.group
        self.run_worker(self.load_scan(), exclusive=True, group="scan")

    async def load_scan(self):
        await self.pause_scan()
        try:
            entries = await asyncio.to_thread(children, self.root) if self.root else await asyncio.to_thread(audit_entries)
            self.scan = Scan(entries, self.timeout)
            self.snapshot = None
            self.tick()
        except OSError as exc:
            self.notify(str(exc), severity="error")

    async def pause_scan(self):
        if self.scan:
            await asyncio.to_thread(self.scan.close)

    def tick(self):
        if not self.scan or not self.is_mounted or len(self.screen_stack) > 1 or not list(self.query("#disk-used")):
            return
        location = self.root or ("/System/Volumes/Data" if Path("/System/Volumes/Data").exists() else "/")
        try:
            disk = shutil.disk_usage(location)
            percent = disk.used / disk.total * 100
            self.query_one("#disk-used", Static).update(f"{size_label(disk.used)}  [dim]{percent:.0f}%[/]")
            self.query_one("#disk-free", Static).update(size_label(disk.free))
            self.query_one("#disk-bar", ProgressBar).update(progress=percent)
        except OSError:
            pass
        measured = sum(entry.size for entry in self.scan.entries)
        self.query_one("#measured", Static).update(size_label(measured))
        done = sum(entry.status not in ("queued", "scanning") for entry in self.scan.entries)
        self.query_one("#scan-status", Static).update(f"{'✓ Scan complete' if self.scan.finished.is_set() else '◐ Scanning'} · {done}/{len(self.scan.entries)} locations")
        snapshot = (self.root, self.group, self.search_text, self.by_size,
                    tuple((entry.path, entry.size, entry.status) for entry in self.scan.entries))
        if snapshot != self.snapshot:
            self.snapshot = snapshot
            self.populate_table()

    def populate_table(self):
        table = self.query_one("#files", DataTable)
        selected = self.active_path
        entries = [entry for entry in self.scan.entries if (self.group == "All" or entry.group == self.group)
                   and self.search_text.casefold() in (entry.name + " " + entry.path).casefold()]
        seen_paths = set()
        deduped = []
        for entry in entries:
            if entry.path not in seen_paths:
                seen_paths.add(entry.path)
                deduped.append(entry)
        entries = deduped
        entries.sort(key=(lambda entry: (-entry.size, entry.name.casefold())) if self.by_size else lambda entry: entry.name.casefold())
        self.visible_entries = {entry.path: entry for entry in entries}
        table.clear()
        peak = max((entry.size for entry in entries), default=1) or 1
        for entry in entries:
            name = Text(f"{entry_icon(entry, bool(self.root))}  {clean(entry.name)}")
            size = Text(size_label(entry.size) + (" *" if entry.status == "partial" else ""), style="#71e5bb")
            size = size if entry.status in ("done", "partial") else Text("—", style="#8395a5")
            bar = Text("━" * round(entry.size / peak * 10), style="#96a6e8")
            status = Text(("✓ Ready" if entry.status == "done" else "◐ Scanning" if entry.status == "scanning" else clean(entry.status)),
                          style="#8395a5" if entry.status in ("queued", "done") else "#edbf75")
            cells = [name, size] + ([bar] if self.show_footprint else []) + [status]
            table.add_row(*cells, key=entry.path)
        if selected in self.visible_entries:
            table.move_cursor(row=list(self.visible_entries).index(selected))
        elif entries:
            table.move_cursor(row=0)
            self.set_detail(entries[0])
        else:
            self.active_path = None
            self.query_one("#selected-path", Static).update("No matching locations. Clear the search or choose another category.")
            self.query_one("#detail-name", Static).update("No matching location")
            self.query_one("#detail-path", Static).update("")
            self.query_one("#detail-size", Static).update("—")
            self.query_one("#explore", Button).disabled = True
        scope = self.group if self.group != "All" else "Overview"
        self.query_one("#scope-title", Label).update(f"{scope}  ·  {len(entries)} locations")
        self.query_one("#scope-path", Static).update(display_path(self.root) if self.root else "Selected locations · allocated sizes · * partial result")

    @on(DataTable.RowHighlighted, "#files")
    def highlight(self, event):
        entry = self.visible_entries.get(event.row_key.value)
        if entry:
            self.set_detail(entry)

    def set_detail(self, entry):
        self.active_path = entry.path
        self.query_one("#detail-name", Static).update(Text(entry.name, style="bold"))
        self.query_one("#detail-size", Static).update(size_label(entry.size) if entry.status in ("done", "partial") else "—")
        self.query_one("#detail-path", Static).update(display_path(entry.path))
        self.query_one("#detail-status", Static).update(Text(f"{entry.group}\n{entry.status}"))
        self.query_one("#selected-path", Static).update(display_path(entry.path))
        self.query_one("#explore", Button).disabled = not Path(entry.path).is_dir()

    @on(DataTable.RowSelected, "#files")
    @on(Button.Pressed, "#explore")
    def explore(self):
        if self.active_path and Path(self.active_path).is_dir():
            self.stack.append((self.root, self.group, self.search_text))
            self.root, self.group, self.search_text = Path(self.active_path), "All", ""
            self.query_one("#search", Input).value = ""
            self.refresh_storage()

    @on(Tabs.TabActivated, "#navigation")
    def navigate(self, event):
        if self.group == event.tab.id:
            return
        self.group = event.tab.id
        self.snapshot = None
        self.tick()

    @on(Input.Changed, "#search")
    def search_changed(self, event):
        self.search_text = event.value
        self.tick()

    @on(Input.Submitted, "#search")
    def search_submitted(self):
        self.query_one("#files", DataTable).focus()

    def action_search(self):
        self.query_one("#search", Input).focus()

    @on(Button.Pressed, "#back")
    def action_back(self):
        if self.stack:
            self.root, self.group, self.search_text = self.stack.pop()
            self.query_one("#search", Input).value = self.search_text
            self.refresh_storage()
        else:
            self.query_one("#search", Input).value = ""
            self.query_one("#files", DataTable).focus()

    @on(Button.Pressed, "#rescan")
    def action_rescan(self):
        self.refresh_storage()

    @on(Button.Pressed, "#sort")
    def action_sort(self):
        self.by_size = not self.by_size
        self.query_one("#sort", Button).label = "↓ Size" if self.by_size else "A–Z Name"
        self.tick()

    @on(Button.Pressed, "#export")
    def action_export(self):
        if self.scan:
            filename = Path.cwd() / ("system-steward-report-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
            try:
                with filename.open("x") as file:
                    json.dump(report(self.scan, self.root), file, indent=2)
                self.notify(f"Saved {filename.name}", title="Report exported")
            except OSError as exc:
                self.notify(str(exc), severity="error")

    @on(Button.Pressed, "#chrome")
    def action_chrome(self):
        self.push_screen(CleanupScreen("Chrome", self.home, self.timeout))

    @on(Button.Pressed, "#adobe")
    def action_adobe(self):
        self.push_screen(CleanupScreen("Adobe", self.home, self.timeout))

    @on(Button.Pressed, "#dashboard-toggle")
    def action_dashboard(self):
        self.dashboard_manual = True
        self.dashboard_expanded = not self.dashboard_expanded
        self.resize_layout(self.size.width, self.size.height)
        if self.dashboard_expanded:
            self.refresh_dashboard()
        else:
            self.query_one("#dashboard").scroll_home(animate=False)

    def refresh_dashboard(self):
        if not self.is_mounted or not self.dashboard_expanded or self.dashboard_sampling or len(self.screen_stack) > 1:
            return
        self.dashboard_sampling = True
        self.run_worker(self.sample_dashboard(), group="dashboard-health", exclusive=True)

    async def sample_dashboard(self):
        dashboard = self.screen_stack[0]
        try:
            data = await asyncio.to_thread(self.dashboard_sampler.sample)
            self.dashboard_sample = data
            for key, value in metric_values(data).items():
                dashboard.query_one(f"#metric-{key}", DashboardCard).set_value(value)
            for key, title, description, tooltip in METRICS:
                detail = tooltip
                if key == "resource":
                    detail = data.get("health_score_msg") or tooltip
                elif key == "network":
                    interfaces = [item.get("name", "") for item in data.get("network") or []]
                    detail += "\nReported interfaces: " + (", ".join(interfaces) or "None")
                dashboard.query_one(f"#metric-{key}").tooltip = detail
            warning = data.get("_warnings")
            status = dashboard.query_one("#dashboard-health-status", Static)
            status.update("△ Some readings are unavailable · open a metric for details." if warning else "Live readings refresh every 5s · select a metric for details.")
            status.tooltip = warning or "CPU, memory, network and power are sampled directly from macOS."
        except (OSError, ValueError, RuntimeError) as exc:
            for key, *_ in METRICS:
                card = dashboard.query_one(f"#metric-{key}", DashboardCard)
                card.set_value("Unavailable")
                card.tooltip = f"Could not refresh this reading: {exc}"
            status = dashboard.query_one("#dashboard-health-status", Static)
            status.update("△ Live readings could not be refreshed · select a metric to retry.")
            status.tooltip = str(exc)
        finally:
            self.dashboard_sampling = False

    @on(Button.Pressed, ".system-metric")
    def open_dashboard_status(self):
        self.open_system_tool("status")

    @on(Button.Pressed, ".feature-card")
    def open_dashboard_tool(self, event):
        self.open_system_tool(event.button.id.removeprefix("tool-"))

    def open_system_tool(self, kind):
        if kind == "awake":
            self.push_screen(KeepAwakeScreen(self.awake, self.root))
            return
        screen = MaintenanceScreen(self.home, self.root, self.timeout, initial_kind=kind)
        self.push_screen(screen)

    @on(Button.Pressed, "#maintenance")
    def action_maintenance(self):
        self.open_system_tool("status")

    @on(Button.Pressed, "#keep-awake")
    def action_keep_awake(self):
        self.open_system_tool("awake")

    def refresh_awake_indicator(self):
        if not self.screen_stack:
            return
        running = self.awake.poll()
        for button in self.screen_stack[0].query("#keep-awake"):
            button.label = "◷  Keep awake · on" if running else "◷  Keep awake"
            button.tooltip = self.awake.status()

    def action_help(self):
        self.push_screen(InfoDialog(f"{APP_NAME} {VERSION} · by {AUTHOR}",
            "Tab / Shift-Tab   Move between panes and controls\n↑ / ↓             Move within a list\nEnter             Explore the highlighted folder\nBackspace         Return to the previous folder\n/                 Search locations\ns                 Sort by size or name\nr                 Rescan\ne                 Export JSON\nc / a             Chrome / Adobe cleanup\nq                 Quit or return from cleanup\n\n"
            "m                 Native system health & maintenance tools\nk                 Keep Awake (macOS caffeinate)\nd                 Expand or collapse the system dashboard\n\n"
            "CONFIRMATION\nReview the selected targets or tasks, then click the action button or press Y. Esc or N cancels. Enter activates the focused button; Cancel is focused by default.\n\n"
            "CLEANUP\nSpace toggles a group. Ctrl-A selects all; Ctrl-D selects none.\nChrome and Adobe cleanup permanently clear the selected contents. System tools move selected targets to Trash.\n\n"
            "Mouse support: click categories, rows, checkboxes and buttons; scroll lists and panels.\n\n"
            "Measurements cover selected locations, not the entire disk. Missing, unreadable, timed-out and partial results are labeled. Cleanup estimates may differ from actual disk space reclaimed."))

    def check_action(self, action, parameters):
        if len(self.screen_stack) > 1 and action in ("search", "rescan", "sort", "back", "chrome", "adobe", "maintenance", "keep_awake", "dashboard", "export", "help"):
            return False
        if isinstance(self.screen, ModalScreen) and action == "quit":
            return False
        return True

    async def action_quit(self):
        screen = self.screen
        if isinstance(screen, MaintenanceScreen) and screen.busy:
            self.notify("Wait for maintenance to finish before quitting.", severity="warning")
            return
        if isinstance(screen, CleanupScreen) and screen.job and not screen.job.done.is_set():
            self.notify("Use Stop after current item before quitting.", severity="warning")
            return
        if isinstance(screen, ModalScreen):
            return
        if self.awake.running and self.awake.plan.mode == "command":
            self.push_screen(MaintenanceConfirm("Quit and stop the running command?",
                "Quitting stops Keep Awake and terminates its wrapped command and process group.", "Quit and stop"),
                lambda approved: self.run_worker(self.finish_quit()) if approved else None)
            return
        await self.finish_quit()

    async def finish_quit(self):
        if not (self.awake.running and getattr(self.awake, "background", False)):
            await asyncio.to_thread(self.awake.stop)
        await self.pause_scan()
        self.exit()

    async def on_unmount(self):
        if not (self.awake.running and getattr(self.awake, "background", False)):
            await asyncio.to_thread(self.awake.stop)
        await self.pause_scan()
