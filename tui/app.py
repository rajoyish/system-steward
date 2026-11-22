"""Main System Steward application: storage explorer, dashboard and tool launchers."""
import asyncio
import datetime
import json
from pathlib import Path
import shutil
import time

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, DataTable, Footer, Input, Label, ProgressBar, Static, Tab, Tabs

from app_identity import APP_NAME, AUTHOR, BRAND, VERSION
from dashboard import DashboardCard, METRICS, TOOLS, metric_values
from keep_awake import AwakeSession
from keep_awake_tui import KeepAwakeScreen
from maintenance_tui import MaintenanceConfirm, MaintenanceScreen
from system_status import SystemSampler
from system_steward import GROUPS, GROUP_ICONS, Scan, audit_entries, children, clean, entry_icon, report, size_label
from tui.cleanup_screen import CleanupScreen
from tui.dialogs import InfoDialog, display_path

# While a scan runs, re-sort the storage table at most this often (seconds); values update every tick.
ORDER_INTERVAL = 1.0
# Stylesheet lives at the project root; Textual resolves relative CSS paths against this file.
STYLESHEET = Path(__file__).resolve().parent.parent / "system_steward.tcss"


class SystemStewardApp(App):
    TITLE = APP_NAME
    SUB_TITLE = f"by {AUTHOR}"
    CSS_PATH = STYLESHEET
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
        self.snapshot = None       # (view key, entry data) last applied to the table; None forces a rebuild
        self.rendered = {}         # path -> signature of the cells currently shown
        self.last_order = 0.0
        self.order_stale = False
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
        data = tuple((entry.path, entry.size, entry.status) for entry in self.scan.entries)
        view = (id(self.scan), self.root, self.group, self.search_text, self.by_size, self.show_footprint)
        if self.snapshot is None or self.snapshot[0] != view:
            self.snapshot = (view, data)
            self.populate_table()
        elif data != self.snapshot[1] or self.order_stale:
            self.snapshot = (view, data)
            self.update_rows()

    def filtered_entries(self):
        needle = self.search_text.casefold()
        seen, entries = set(), []
        for entry in self.scan.entries:
            if entry.path in seen or (self.group != "All" and entry.group != self.group):
                continue
            if needle in (entry.name + " " + entry.path).casefold():
                seen.add(entry.path)
                entries.append(entry)
        entries.sort(key=(lambda entry: (-entry.size, entry.name.casefold())) if self.by_size else lambda entry: entry.name.casefold())
        return entries

    def row_cells(self, entry, peak):
        """(signature, {column: cell}) for one row; the signature detects which cells changed."""
        bar_length = round(entry.size / peak * 10)
        size = Text(size_label(entry.size) + (" *" if entry.status == "partial" else ""), style="#71e5bb")
        size = size if entry.status in ("done", "partial") else Text("—", style="#8395a5")
        status = Text(("✓ Ready" if entry.status == "done" else "◐ Scanning" if entry.status == "scanning" else clean(entry.status)),
                      style="#8395a5" if entry.status in ("queued", "done") else "#edbf75")
        cells = {"size": size, "status": status}
        if self.show_footprint:
            cells["share"] = Text("━" * bar_length, style="#96a6e8")
        return (entry.size, entry.status, bar_length), cells

    def update_rows(self):
        """Apply scan progress without rebuilding the table (sorting at most once per second)."""
        entries = self.filtered_entries()
        if [entry.path for entry in entries] != list(self.visible_entries):
            if self.scan.finished.is_set() or time.monotonic() - self.last_order >= ORDER_INTERVAL:
                self.populate_table()
                return
            self.order_stale = True  # keep the current order for now; refresh values in place
        else:
            self.order_stale = False
        table = self.query_one("#files", DataTable)
        peak = max((entry.size for entry in entries), default=1) or 1
        for entry in entries:
            signature, cells = self.row_cells(entry, peak)
            previous = self.rendered.get(entry.path)
            if previous == signature:
                continue
            self.rendered[entry.path] = signature
            for column, cell in cells.items():
                table.update_cell(entry.path, column, cell)
            if entry.path == self.active_path:
                self.set_detail(entry)

    def populate_table(self):
        table = self.query_one("#files", DataTable)
        selected = self.active_path
        entries = self.filtered_entries()
        self.visible_entries = {entry.path: entry for entry in entries}
        self.last_order, self.order_stale, self.rendered = time.monotonic(), False, {}
        table.clear()
        peak = max((entry.size for entry in entries), default=1) or 1
        for entry in entries:
            name = Text(f"{entry_icon(entry, bool(self.root))}  {clean(entry.name)}")
            signature, cells = self.row_cells(entry, peak)
            self.rendered[entry.path] = signature
            row = [name, cells["size"]] + ([cells["share"]] if self.show_footprint else []) + [cells["status"]]
            table.add_row(*row, key=entry.path)
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
        # The indicator lives on the base screen; skip work while another screen covers it.
        if len(self.screen_stack) != 1:
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
