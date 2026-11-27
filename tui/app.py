"""Main System Steward application: a live dashboard and a storage explorer, plus tool launchers."""
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
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, ContentSwitcher, DataTable, Footer, Input, Label, Static, Tab, Tabs

from app_identity import APP_NAME, AUTHOR, BRAND, VERSION
from dashboard import (BAD, CLEANUP_TOOLS, DIM, GOOD, INFO, METRICS, MUTED, TOOLS, WARN, DashboardCard, DashboardView,
                       meter, metric_cards)
from keep_awake import AwakeSession
from keep_awake_tui import KeepAwakeScreen
from maintenance_tui import MaintenanceConfirm, MaintenanceScreen
from strings import TEXT
from system_status import SystemSampler
from system_steward import GROUP_ICONS, GROUPS, Scan, audit_entries, children, clean, entry_icon, report, size_label
from tui.cleanup_screen import CleanupScreen
from tui.dialogs import InfoDialog, UpdateDialog, display_path
import updater

# While a scan runs, re-sort the storage table at most this often (seconds); values update every tick.
ORDER_INTERVAL = 1.0
# Stylesheet lives at the project root; Textual resolves relative CSS paths against this file.
STYLESHEET = Path(__file__).resolve().parent.parent / "system_steward.tcss"
# Main-screen actions that only make sense in the storage view (hidden from the footer elsewhere).
STORAGE_ACTIONS = ("sort", "export", "back", "category", "escape")
BASE_ACTIONS = ("search", "rescan", "sort", "back", "chrome", "adobe", "maintenance", "keep_awake", "dashboard",
                "export", "help", "view", "category", "escape")
METRIC_HINTS = {"resource": "m", "awake": "k"}


class FilesTable(DataTable):
    """Storage table: ← goes up a folder, → / Enter opens the highlighted folder."""
    BINDINGS = [Binding("left", "app.back", "Up", show=False), Binding("right", "app.open", "Open", show=False)]


class SystemStewardApp(App):
    TITLE = APP_NAME
    SUB_TITLE = f"by {AUTHOR}"
    CSS_PATH = STYLESHEET
    BINDINGS = [Binding("1", "view('dashboard')", "Dashboard"), Binding("2", "view('storage')", "Storage"),
                Binding("d", "dashboard", "Switch view", show=False),
                Binding("/", "search", "Search"), Binding("s", "sort", "Sort"), Binding("e", "export", "Export"),
                Binding("backspace", "back", "Up"), Binding("escape", "escape", "Back", show=False),
                Binding("r", "rescan", "Refresh"),
                Binding("[", "category(-1)", "Previous category", show=False),
                Binding("]", "category(1)", "Next category", show=False),
                Binding("c", "chrome", "Chrome", show=False), Binding("a", "adobe", "Adobe", show=False),
                Binding("m", "maintenance", "System tools", show=False),
                Binding("k", "keep_awake", "Keep awake", show=False),
                Binding("?", "help", "Help"), Binding("q", "quit", "Quit"), Binding("ctrl+c", "quit", "Quit", show=False)]

    def __init__(self, root=None, timeout=120, home=None, check_updates=False):
        super().__init__()
        self.root, self.timeout, self.home = root, timeout, home
        self.check_updates = check_updates
        self.pending_update = None  # set when the person chooses "Update now"; main() applies it after exit
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
        self.view = "storage" if root else "dashboard"
        self.dashboard_sampling = False
        self.dashboard_sampler = SystemSampler()
        self.dashboard_sample = None
        self.painted = {}          # widget id -> last content, to skip redundant updates
        self.awake = AwakeSession()
        self.register_theme(Theme(name="system-steward", primary="#71e5bb", secondary="#96a6e8", accent="#96a6e8",
                                  foreground="#dde7ef", background="#0c1218", surface="#121c25", panel="#17232e",
                                  success="#71e5bb", warning="#edbf75", error="#f18087", dark=True))
        self.theme = "system-steward"

    @property
    def dashboard_expanded(self):
        return self.view == "dashboard"

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar", classes="topbar"):
            with Vertical(id="app-identity"):
                brand = Label(BRAND, classes="brand")
                brand.tooltip = f"{APP_NAME} {VERSION}"
                yield brand
                yield Label(f"by {AUTHOR}", id="author")
            yield Tabs(Tab("Dashboard  1", id="view-dashboard"), Tab("Storage  2", id="view-storage"),
                       active=f"view-{self.view}", id="views")
            yield Button("◷ Keep awake", id="keep-awake", compact=True)
        with ContentSwitcher(id="view-switcher", initial=self.view):
            yield from self.compose_dashboard()
            yield from self.compose_storage()
        yield Static("Enlarge the terminal to at least 64 × 22.", id="too-small")
        yield Footer()

    def compose_dashboard(self) -> ComposeResult:
        with DashboardView(id="dashboard", can_focus=False):
            with Horizontal(id="dashboard-heading", classes="section-heading"):
                yield Label("SYSTEM", classes="eyebrow")
                yield Static("Reading live system data…", id="dashboard-health-status")
            with Grid(id="metric-grid", classes="card-grid"):
                for key, title, caption, tooltip in METRICS:
                    yield DashboardCard(title, caption, id=f"metric-{key}", classes="system-metric",
                                        tooltip=tooltip, key_hint=METRIC_HINTS.get(key))
            with Horizontal(id="dashboard-panels"):
                with Vertical(id="processes-panel", classes="panel"):
                    yield Label("TOP PROCESSES", classes="eyebrow")
                    yield DataTable(id="top-processes", cursor_type="row", zebra_stripes=True)
                with Vertical(id="storage-panel", classes="panel"):
                    with Horizontal(classes="section-heading"):
                        yield Label("STORAGE", classes="eyebrow")
                        yield Button("Explore  2 →", id="open-storage", compact=True)
                    yield Static("Measuring…", id="storage-breakdown")
            with Horizontal(classes="section-heading"):
                yield Label("TOOLS", classes="eyebrow")
                yield Static("Every tool previews its changes before anything is removed.", classes="section-note")
            with Grid(id="tool-grid", classes="card-grid"):
                for key, title, description, hint in CLEANUP_TOOLS:
                    yield DashboardCard(title, description, id=f"tool-{key}", classes="feature-card", key_hint=hint)
                for key, title, description in TOOLS:
                    yield DashboardCard(title, description, id=f"tool-{key}", classes="feature-card")

    def compose_storage(self) -> ComposeResult:
        with Vertical(id="storage"):
            with Vertical(id="main-panel", classes="panel"):
                with Horizontal(id="table-toolbar"):
                    with Vertical(id="scope"):
                        yield Label("Overview", id="scope-title")
                        yield Static("", id="scope-path")
                    yield Input(placeholder="Search  /", id="search")
                with Horizontal(id="explore-navigation"):
                    yield Tabs(*(Tab(f"{GROUP_ICONS[group]} {group if group != 'All' else 'All'}", id=group)
                                 for group in GROUPS), active="All", id="navigation")
                with Horizontal(id="inspector-panes"):
                    yield FilesTable(id="files", cursor_type="row", zebra_stripes=True, show_row_labels=False)
                    with VerticalScroll(id="details"):
                        yield Static("Select a location", id="detail-name")
                        yield Static("—", id="detail-size", classes="metric-value")
                        yield Static("", id="detail-status")
                        yield Static("", id="detail-path")
                        yield Button("Open folder  ↵", id="explore", disabled=True, compact=True)
                with Horizontal(id="table-actions"):
                    yield Button("← Up", id="back", compact=True, tooltip="Go up to the previous folder (Backspace or ←)")
                    yield Button("↻ Rescan", id="rescan", compact=True, tooltip="Measure these locations again (r)")
                    yield Button("Sort: size", id="sort", compact=True, tooltip="Sort by size or name (s)")
                    yield Button("Export JSON", id="export", compact=True, tooltip="Save a JSON report to the current folder (e)")
                    yield Static("↑↓ select · ↵ open · ← up · [ ] category", id="storage-hint")

    def on_mount(self):
        self.configure_columns()
        self.query_one("#top-processes", DataTable).add_columns("PROCESS", "CPU %", "MEM %")
        self.refresh_storage()
        self.set_interval(.25, self.tick)
        self.resize_layout(self.size.width, self.size.height)
        self.set_interval(5, self.refresh_dashboard)
        self.set_interval(1, self.refresh_awake_indicator)
        self.action_view(self.view, force=True)
        self.refresh_awake_indicator()
        if self.check_updates and updater.checks_enabled():
            self.run_worker(self.look_for_update(), exclusive=True, group="update")

    # ── Updates ────────────────────────────────────────────────────────────

    async def look_for_update(self, channel=None):
        update = await asyncio.to_thread(updater.check, channel)
        if update is not None:
            self.offer_update(update)

    def offer_update(self, update):
        if len(self.screen_stack) > 1:  # never interrupt a tool or dialog; ask when back on the main screen
            self.set_timer(2, lambda: self.offer_update(update))
            return
        windows_channel = update.channel.name in ("scoop", "winget")
        self.push_screen(UpdateDialog(update, awake_running=windows_channel and self.awake.poll()),
                         lambda choice: self.update_chosen(update, choice))

    def update_chosen(self, update, choice):
        if choice == "skip":
            updater.skip_version(update.latest)
            self.notify(f"Version {update.latest} skipped. Run system-steward --update to install it later.")
        elif choice == "update":
            self.pending_update = update
            self.run_worker(self.action_quit())

    # ── Views ──────────────────────────────────────────────────────────────

    def action_view(self, name, force=False):
        if name == self.view and not force:
            return
        self.view = name
        self.query_one("#view-switcher", ContentSwitcher).current = name
        tabs = self.query_one("#views", Tabs)
        with tabs.prevent(Tabs.TabActivated):
            tabs.active = f"view-{name}"
        self.set_class(name == "dashboard", "dashboard-expanded")
        if name == "dashboard":
            self.query_one("#metric-disk").focus()
            self.refresh_dashboard()
            self.painted.pop("storage-breakdown", None)
        else:
            self.query_one("#files", DataTable).focus()
        self.refresh_bindings()

    def action_dashboard(self):
        self.action_view("storage" if self.view == "dashboard" else "dashboard")

    @on(Tabs.TabActivated, "#views")
    def view_tab(self, event):
        self.action_view(event.tab.id.removeprefix("view-"))

    @on(Button.Pressed, "#open-storage")
    def open_storage(self):
        self.action_view("storage")

    # ── Layout ─────────────────────────────────────────────────────────────

    def configure_columns(self):
        table = self.query_one("#files", DataTable)
        table.clear(columns=True)
        table.add_column("LOCATION", key="name", width=30)
        table.add_column("SIZE", key="size", width=11)
        if self.show_footprint:
            table.add_column("SHARE", key="share", width=16)
        table.add_column("STATUS", key="status", width=12)

    def on_resize(self, event):
        self.resize_layout(event.size.width, event.size.height)

    def resize_layout(self, width, height):
        self.set_class(width < 100, "narrow")
        self.set_class(width < 125, "compact")
        self.set_class(height < 34, "short-dashboard")
        self.set_class(width < 64 or height < 22, "tiny")
        if self.is_mounted and self.show_footprint != (width >= 85):
            self.show_footprint = width >= 85
            self.configure_columns()
            self.snapshot = None
            self.tick()

    def paint(self, identifier, content, widget=None):
        """Update a Static only when its content changed."""
        key = repr(content) if isinstance(content, Text) else content
        if self.painted.get(identifier) != key:
            self.painted[identifier] = key
            (widget or self.query_one(f"#{identifier}", Static)).update(content)

    # ── Storage scan ───────────────────────────────────────────────────────

    def refresh_storage(self):
        if self.root and not self.root.is_dir():
            self.root, self.group, self.stack = None, "All", []
        navigation = self.query_one("#navigation", Tabs)
        with navigation.prevent(Tabs.TabActivated):
            navigation.active = self.group
        self.query_one("#back", Button).disabled = not self.stack and not self.search_text
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
        if not self.scan or not self.is_mounted or len(self.screen_stack) > 1:
            return
        try:
            self.apply_scan_progress()
        except NoMatches:
            pass  # widgets are being torn down (app exit); nothing to update

    def apply_scan_progress(self):
        data = tuple((entry.path, entry.size, entry.status) for entry in self.scan.entries)
        view = (id(self.scan), self.root, self.group, self.search_text, self.by_size, self.show_footprint)
        if self.snapshot is None or self.snapshot[0] != view:
            self.snapshot = (view, data)
            self.populate_table()
        elif data != self.snapshot[1] or self.order_stale:
            self.snapshot = (view, data)
            self.update_rows()
        self.update_scope()
        if self.view == "dashboard":
            self.update_breakdown()

    def scan_progress(self):
        done = sum(entry.status not in ("queued", "scanning") for entry in self.scan.entries)
        return done, len(self.scan.entries), self.scan.finished.is_set()

    def update_scope(self):
        entries = list(self.visible_entries.values())
        total = sum(entry.size for entry in entries)
        done, count, finished = self.scan_progress()
        scope = self.group if self.group != "All" else (self.root.name or str(self.root) if self.root else "Common locations")
        title = Text(f"{scope}", style="bold")
        title.append(f"   {len(entries)} items · {size_label(total)}", style=INFO)
        if not finished:
            title.append(f"   ◐ measuring {done}/{count}", style=WARN)
        self.paint("scope-title", title, self.query_one("#scope-title", Label))
        where = display_path(self.root) if self.root else Text("App caches, developer folders and projects")
        location = self.root or ("/System/Volumes/Data" if Path("/System/Volumes/Data").exists() else Path.home().anchor or "/")
        try:
            disk = shutil.disk_usage(location)
            where.append(f"  ·  {size_label(disk.free)} free of {size_label(disk.total)}", style=INFO)
        except OSError:
            pass
        if any(entry.status == "partial" for entry in entries):
            where.append("  ·  * partial size", style=WARN)
        self.paint("scope-path", where)

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

    def row_cells(self, entry, peak, total):
        """(signature, {column: cell}) for one row; the signature detects which cells changed."""
        bar_length = round(entry.size / peak * 10)
        share = round(entry.size / total * 100) if total else 0
        measured = entry.status in ("done", "partial")
        size = Text(size_label(entry.size) + (" *" if entry.status == "partial" else ""), style=GOOD) if measured else Text("—", style=DIM)
        label = {"done": "", "partial": "partial", "scanning": "◐ measuring", "queued": "queued"}.get(entry.status, clean(entry.status))
        status = Text(label, style=DIM if entry.status in ("queued", "scanning") else WARN)
        cells = {"size": size, "status": status}
        if self.show_footprint:
            bar = Text("━" * bar_length, style="#96a6e8")
            bar.append("━" * (10 - bar_length), style="#1f2e3a")
            bar.append(f" {share:>3}%", style=MUTED)
            cells["share"] = bar
        return (entry.size, entry.status, bar_length, share), cells

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
        total = sum(entry.size for entry in entries)
        for entry in entries:
            signature, cells = self.row_cells(entry, peak, total)
            if self.rendered.get(entry.path) == signature:
                continue
            self.rendered[entry.path] = signature
            for column, cell in cells.items():
                table.update_cell(entry.path, column, cell)
            if entry.path == self.active_path:
                self.set_detail(entry)

    def populate_table(self):
        table = self.query_one("#files", DataTable)
        selected = self.active_path
        # While measuring, a cursor resting on the first row follows whatever sorts to the top.
        if table.cursor_row == 0 and not self.scan.finished.is_set():
            selected = None
        entries = self.filtered_entries()
        self.visible_entries = {entry.path: entry for entry in entries}
        self.last_order, self.order_stale, self.rendered = time.monotonic(), False, {}
        table.clear()
        peak = max((entry.size for entry in entries), default=1) or 1
        total = sum(entry.size for entry in entries)
        for entry in entries:
            name = Text(f"{entry_icon(entry, bool(self.root))}  {clean(entry.name)}")
            signature, cells = self.row_cells(entry, peak, total)
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
            message = "No matches. Press Esc to clear the search." if self.search_text else "Nothing here yet."
            self.query_one("#detail-name", Static).update(message)
            self.query_one("#detail-path", Static).update("")
            self.query_one("#detail-status", Static).update("")
            self.query_one("#detail-size", Static).update("—")
            self.query_one("#explore", Button).disabled = True

    @on(DataTable.RowHighlighted, "#files")
    def highlight(self, event):
        entry = self.visible_entries.get(event.row_key.value)
        if entry:
            self.set_detail(entry)

    def set_detail(self, entry):
        self.active_path = entry.path
        total = sum(item.size for item in self.visible_entries.values())
        measured = entry.status in ("done", "partial")
        self.query_one("#detail-name", Static).update(Text(entry.name, style="bold"))
        self.query_one("#detail-size", Static).update(size_label(entry.size) if measured else "—")
        facts = []
        if measured and total:
            facts.append(f"{entry.size / total * 100:.0f}% of this view")
        if entry.group not in ("Folder contents",):
            facts.append(entry.group)
        if entry.status != "done":
            facts.append(clean(entry.status))
        self.query_one("#detail-status", Static).update(Text(" · ".join(facts)))
        self.query_one("#detail-path", Static).update(display_path(entry.path))
        self.query_one("#explore", Button).disabled = not Path(entry.path).is_dir()

    @on(DataTable.RowSelected, "#files")
    @on(Button.Pressed, "#explore")
    def explore(self):
        if self.active_path and Path(self.active_path).is_dir():
            self.stack.append((self.root, self.group, self.search_text))
            self.root, self.group, self.search_text = Path(self.active_path), "All", ""
            self.query_one("#search", Input).value = ""
            self.refresh_storage()

    def action_open(self):
        self.explore()

    @on(Tabs.TabActivated, "#navigation")
    def navigate(self, event):
        if self.group == event.tab.id:
            return
        self.group = event.tab.id
        self.snapshot = None
        self.tick()

    def action_category(self, step):
        index = (GROUPS.index(self.group) + step) % len(GROUPS)
        self.query_one("#navigation", Tabs).active = GROUPS[index]

    @on(Input.Changed, "#search")
    def search_changed(self, event):
        self.search_text = event.value
        self.query_one("#back", Button).disabled = not self.stack and not self.search_text
        self.tick()

    @on(Input.Submitted, "#search")
    def search_submitted(self):
        self.query_one("#files", DataTable).focus()

    def action_search(self):
        if self.view != "storage":
            self.action_view("storage")
        self.query_one("#search", Input).focus()

    def action_escape(self):
        search = self.query_one("#search", Input)
        if self.focused is search or search.value:
            search.value = ""
            self.query_one("#files", DataTable).focus()
        elif self.stack:
            self.action_back()

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
        if self.view == "dashboard":
            self.refresh_dashboard()

    @on(Button.Pressed, "#sort")
    def action_sort(self):
        self.by_size = not self.by_size
        self.query_one("#sort", Button).label = "Sort: size" if self.by_size else "Sort: name"
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

    # ── Dashboard ──────────────────────────────────────────────────────────

    def update_breakdown(self):
        widget = self.query_one("#storage-breakdown", Static)
        width = max(10, widget.size.width or 40)
        sizes, seen = {}, set()
        for entry in self.scan.entries:
            if entry.path not in seen:
                seen.add(entry.path)
                sizes[entry.group] = sizes.get(entry.group, 0) + entry.size
        done, count, finished = self.scan_progress()
        total = sum(sizes.values())
        disks = (self.dashboard_sample or {}).get("disks") or []
        signature = (width, tuple(sorted(sizes.items())), done, finished, tuple((d.get("mount"), d.get("used")) for d in disks))
        if self.painted.get("storage-breakdown") == signature:
            return
        self.painted["storage-breakdown"] = signature
        text = Text()
        text.append(size_label(total), style=f"bold {GOOD}")
        text.append(" measured" + ("" if self.root is None else f" in {self.root.name}"), style=INFO)
        if not finished:
            text.append(f"  ◐ {done}/{count}", style=WARN)
        bar_width = max(4, width - 25)
        peak = max(sizes.values(), default=1) or 1
        for group, size in sorted(sizes.items(), key=lambda item: -item[1]):
            text.append(f"\n{GROUP_ICONS.get(group, '•')} {group[:10]:<10} ")
            filled = round(size / peak * bar_width)
            text.append("━" * filled, style="#96a6e8").append("━" * (bar_width - filled), style="#1f2e3a")
            text.append(f" {size_label(size):>9}", style=MUTED)
        if disks:
            text.append("\n\nDRIVES", style=f"bold {MUTED}")
            for disk in disks:
                percent = disk.get("used_percent") or 0
                mount = str(disk.get("mount", "?"))
                text.append(f"\n{(Path(mount).name or mount)[:10]:<12}")
                text.append_text(meter(percent, bar_width))
                text.append(f" {percent:>4.0f}%", style=MUTED)
        widget.update(text)

    def refresh_dashboard(self):
        if not self.is_mounted or self.view != "dashboard" or self.dashboard_sampling or len(self.screen_stack) > 1:
            return
        self.dashboard_sampling = True
        self.run_worker(self.sample_dashboard(), group="dashboard-health", exclusive=True)

    async def sample_dashboard(self):
        dashboard = self.screen_stack[0]
        status = dashboard.query_one("#dashboard-health-status", Static)
        try:
            data = await asyncio.to_thread(self.dashboard_sampler.sample)
            self.dashboard_sample = data
            for key, (value, caption, percent, inverted) in metric_cards(data).items():
                dashboard.query_one(f"#metric-{key}", DashboardCard).set_value(value, caption, percent, inverted)
            for key, _title, _caption, tooltip in METRICS:
                detail = tooltip
                if key == "resource":
                    detail = data.get("health_score_msg") or tooltip
                elif key == "network":
                    interfaces = [item.get("name", "") for item in data.get("network") or []]
                    detail += "\nReported interfaces: " + (", ".join(interfaces) or "None")
                dashboard.query_one(f"#metric-{key}").tooltip = detail
            self.update_processes(data.get("top_processes") or [])
            self.painted.pop("storage-breakdown", None)
            warning = data.get("_warnings")
            stamp = datetime.datetime.now().strftime("%H:%M:%S")
            line = Text(f"Live · updated {stamp} · every 5s · Enter on a card opens details", style=INFO)
            if warning:
                line = Text(f"△ Some readings unavailable · updated {stamp} · open a card for details", style=WARN)
            status.update(line)
            status.tooltip = warning or TEXT.sampled_from
        except (OSError, ValueError, RuntimeError) as exc:
            for key, *_ in METRICS:
                if key == "awake":
                    continue
                card = dashboard.query_one(f"#metric-{key}", DashboardCard)
                card.set_value("Unavailable")
                card.tooltip = f"Could not refresh this reading: {exc}"
            status.update(Text("△ Live readings could not be refreshed · press r to retry.", style=BAD))
            status.tooltip = str(exc)
        finally:
            self.dashboard_sampling = False

    def update_processes(self, processes):
        table = self.query_one("#top-processes", DataTable)
        row = table.cursor_row
        table.clear()
        for item in processes[:8]:
            name = Path(str(item.get("name", "?"))).name
            cpu, memory = item.get("cpu"), item.get("memory")
            table.add_row(Text(clean(name)),
                          Text(f"{cpu:5.1f}" if isinstance(cpu, (int, float)) else "—", style=WARN if (cpu or 0) >= 50 else GOOD),
                          Text(f"{memory:5.1f}" if isinstance(memory, (int, float)) else "—", style=MUTED))
        if not processes:
            table.add_row(Text("Waiting for the next sample…", style=MUTED), "", "")
        if table.row_count:
            table.move_cursor(row=min(row, table.row_count - 1))

    @on(DataTable.RowSelected, "#top-processes")
    def open_processes(self):
        self.open_system_tool("status")

    @on(Button.Pressed, ".system-metric")
    def open_dashboard_status(self, event):
        self.open_system_tool("awake" if event.button.id == "metric-awake" else "status")

    @on(Button.Pressed, ".feature-card")
    def open_dashboard_tool(self, event):
        key = event.button.id.removeprefix("tool-")
        if key == "chrome":
            self.action_chrome()
        elif key == "adobe":
            self.action_adobe()
        else:
            self.open_system_tool(key)

    # ── Tools ──────────────────────────────────────────────────────────────

    def action_chrome(self):
        self.push_screen(CleanupScreen("Chrome", self.home, self.timeout))

    def action_adobe(self):
        self.push_screen(CleanupScreen("Adobe", self.home, self.timeout))

    def open_system_tool(self, kind):
        if kind == "awake":
            self.push_screen(KeepAwakeScreen(self.awake, self.root))
            return
        self.push_screen(MaintenanceScreen(self.home, self.root, self.timeout, initial_kind=kind))

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
        status = self.awake.status()
        base = self.screen_stack[0]
        for button in base.query("#keep-awake"):
            button.label = "◷ Keep awake · on" if running else "◷ Keep awake"
            button.set_class(running, "-on")
            button.tooltip = status
        if self.painted.get("metric-awake") != (running, status):
            self.painted["metric-awake"] = (running, status)
            summary = str(status).splitlines()[0] if status else ""
            base.query_one("#metric-awake", DashboardCard).set_value("On" if running else "Off", summary or None)

    def action_help(self):
        self.push_screen(InfoDialog(f"{APP_NAME} {VERSION} · by {AUTHOR}",
            "VIEWS\n"
            "1 / 2             Dashboard / Storage (or click the tabs at the top)\n"
            "d                 Switch between the two views\n"
            "Tab / Shift-Tab   Move between controls\n\n"
            "DASHBOARD\n"
            "Arrow keys        Move between cards\n"
            "Enter / click     Open the card's details or tool\n"
            "r                 Refresh readings\n\n"
            "STORAGE\n"
            "↑ / ↓             Select a location\n"
            "Enter / →         Open the highlighted folder\n"
            "Backspace / ←     Go up to the previous folder\n"
            "[ / ]             Previous / next category\n"
            "/                 Search (Esc clears it)\n"
            "s · r · e         Sort · Rescan · Export JSON\n\n"
            "TOOLS (from anywhere)\n"
            f"c / a             Chrome / Adobe cleanup\nm                 System status & maintenance\nk                 Keep Awake ({TEXT.keep_awake_engine})\n"
            "q                 Quit, or leave a tool screen (also Esc)\n\n"
            "CLEANUP & CONFIRMATION\n"
            "Space toggles a group. Ctrl-A selects all; Ctrl-D selects none. Review the selection, then press Y or click the action button; "
            "Esc or N cancels. Cancel is focused by default, so Enter alone never deletes.\n"
            f"Chrome and Adobe cleanup permanently clear the selected contents. System tools move selected targets to {TEXT.trash}.\n\n"
            "ABOUT THE NUMBERS\n"
            "Storage covers selected locations, not the entire disk. Directory sizes use allocated disk blocks; "
            "a * marks a partial result that can undercount. Cleanup estimates may differ from the space actually reclaimed."))

    def check_action(self, action, parameters):
        if len(self.screen_stack) > 1 and action in BASE_ACTIONS:
            return False
        if action in STORAGE_ACTIONS and self.view != "storage":
            return False
        if action == "view" and parameters and parameters[0] == self.view:
            return None  # shown, dimmed: already here
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
