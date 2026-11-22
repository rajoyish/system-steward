"""Chrome / Adobe cleanup screen: discover, measure, select, confirm, clear."""
import asyncio
from pathlib import Path

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Label, ProgressBar, Select, SelectionList, Static

import adobe_cleanup
import chrome_cleanup
from app_identity import BRAND
from cleanup_selection import cleanup_groups, selection_plan
from system_steward import GROUP_ICONS, Entry, Scan, pretty_path, size_label
from tui.dialogs import CleanupList, ConfirmDialog, InfoDialog

# Seconds to wait for a cleanup worker to finish its current location when the screen closes.
JOB_EXIT_GRACE = 5


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
            # The job stops after its current location. Bound the wait so one huge cache folder
            # cannot hang app exit; the worker is a daemon thread and ends with the process.
            await asyncio.to_thread(self.job.thread.join, JOB_EXIT_GRACE)
