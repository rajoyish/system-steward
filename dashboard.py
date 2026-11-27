"""Dashboard cards for native resource readings and maintenance shortcuts."""
import os
import sys

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Button

from system_steward import clean, size_label

# Shared palette (mirrors system_steward.tcss).
MUTED, DIM, TRACK = "#8395a5", "#526b7e", "#293b4a"
INFO = "#66798a"  # secondary info text: captions, hints, notes
GOOD, WARN, BAD, TEXT_COLOR = "#71e5bb", "#edbf75", "#f18087", "#dde7ef"


def tools(platform=None):
    """Dashboard tool cards for ``platform``: (key, title, description)."""
    if (platform or sys.platform) == "win32":
        # Uninstall Apps and Touch ID are macOS-only; Windows gets status and Keep Awake instead.
        return (
            ("clean", "Clean system", "Temporary files and caches"),
            ("purge", "Developer artifacts", "Dependencies and build output"),
            ("installer", "Clean installers", "EXE, MSI and ISO downloads"),
            ("optimize", "System maintenance", "Flush DNS, icons, Store, Search"),
            ("status", "System status", "CPU, memory, disks, processes"),
            ("awake", "Keep awake", "Prevent sleep for a while"),
        )
    return (
        ("clean", "Clean system", "App caches and logs"),
        ("purge", "Developer artifacts", "Dependencies and build output"),
        ("installer", "Clean installers", "DMG, PKG and ISO downloads"),
        ("uninstall", "Uninstall apps", "Apps and their leftover files"),
        ("optimize", "System maintenance", "DNS, previews, apps, search"),
        ("touchid", "Touch ID for sudo", "Fingerprint access for sudo"),
    )


# Cleanup screens shown beside the native tools, with the key that opens each one.
CLEANUP_TOOLS = (("chrome", "Chrome cleanup", "Browser caches and site data", "c"),
                 ("adobe", "Adobe caches", "Media and app caches", "a"))


def metrics(platform=None):
    """Stat tiles: (key, title, fallback caption, tooltip)."""
    windows = (platform or sys.platform) == "win32"
    memory = ("Physical RAM in use by processes and the system. Standby cache that Windows can reclaim is excluded."
              if windows else
              "Physical RAM used by active pages, wired pages and the compressor. Inactive cache is excluded.")
    return (
        ("disk", "STARTUP DISK", "Space used", "Used and free space on the startup volume."),
        ("cpu", "CPU", "Processor activity", "Percentage of CPU time spent working, measured between native system samples."),
        ("memory", "MEMORY", "RAM in use", memory),
        ("resource", "HEADROOM", "CPU, RAM & disk", "An app-defined resource estimate, not a hardware diagnosis. Open system status for the calculation and readings."),
        ("network", "NETWORK", "Busiest interface", "Incoming and outgoing data rates on the busiest measured interface. The first reading waits for the next sample. The caption shows bytes received and sent since the counters started."),
        ("power", "BATTERY", "Charge & status", "Battery percentage and charging state. " + (
            "Desktop PCs may have no battery." if windows else "Desktop Macs may have no internal battery.")),
        ("uptime", "UPTIME", "Since last boot", "Time since the system last started."),
        ("awake", "KEEP AWAKE", "Sleep prevention", "Prevent the computer from sleeping. Opens Keep Awake."),
    )


TOOLS = tools()
METRICS = metrics()


def tone(percent, inverted=False):
    """Green / amber / red for a usage percentage (``inverted``: higher is better)."""
    if percent is None:
        return GOOD
    value = 100 - percent if inverted else percent
    return GOOD if value < 70 else WARN if value < 85 else BAD


def meter(percent, width, color=None):
    """A text bar ``width`` cells wide, filled to ``percent``."""
    percent = max(0.0, min(100.0, float(percent)))
    filled = round(percent / 100 * width)
    bar = Text("━" * filled, style=color or tone(percent))
    bar.append("━" * (width - filled), style=TRACK)
    return bar


class DashboardCard(Button):
    """A keyboard- and mouse-accessible card: title, value, optional meter and caption."""
    def __init__(self, title, description, *, id, classes="", tooltip=None, key_hint=None):
        self.card_title, self.description, self.key_hint = title, description, key_hint
        self.value = self.caption = self.percent = None
        self.inverted = False
        super().__init__(id=id, classes=classes, tooltip=tooltip or description)
        self.render_card()

    def set_value(self, value, caption=None, percent=None, inverted=False):
        self.value, self.caption, self.percent, self.inverted = value, caption, percent, inverted
        self.render_card()

    def on_resize(self):
        self.render_card()

    def render_card(self):
        label = Text(self.card_title, style=f"bold {MUTED}")
        if self.key_hint:
            label.append(f"  {self.key_hint}", style=DIM)
        if self.value is not None:
            color = tone(self.percent, self.inverted) if self.percent is not None else GOOD
            label.append("\n" + clean(self.value), style=f"bold {color if self.value != 'Unavailable' else MUTED}")
        if self.percent is not None:
            width = max(6, (self.size.width or 24) - 4)
            label.append("\n").append_text(meter(self.percent, width, tone(self.percent, self.inverted)))
        label.append("\n" + clean(self.caption or self.description), style=INFO)
        self.label = label


class DashboardView(VerticalScroll):
    """Scrollable dashboard; arrow keys move between cards laid out in rows."""
    BINDINGS = [Binding("up", "move(0, -1)", "Move", show=False), Binding("down", "move(0, 1)", "Move", show=False),
                Binding("left", "move(-1, 0)", "Move", show=False), Binding("right", "move(1, 0)", "Move", show=False)]

    def canvas_region(self, widget):
        """``widget``'s region in this view's scrollable canvas (virtual regions are parent-relative)."""
        region, node = widget.virtual_region, widget.parent
        while node is not None and node is not self:
            region = region.translate(node.virtual_region.offset)
            node = node.parent
        return region

    def action_move(self, dx, dy):
        # Positions within the scrollable canvas, so cards scrolled out of view still count and the
        # grids may reflow (4 columns wide, 2 narrow) without changing this logic.
        cards = [(card, self.canvas_region(card)) for card in self.query(DashboardCard) if card.display]
        cards = [(card, region) for card, region in cards if region.width]
        if not cards:
            return
        cards.sort(key=lambda item: (item[1].y, item[1].x))
        focused = self.app.focused
        current = next((region for card, region in cards if card is focused), None)
        if current is None:
            cards[0][0].focus()
            return
        if dx:
            # Left/right walk the cards in reading order, wrapping between rows.
            index = [card for card, _ in cards].index(focused) + dx
            if 0 <= index < len(cards):
                cards[index][0].focus()
            return
        middle = current.x + current.width / 2
        if dy > 0:
            below = [(card, region) for card, region in cards if region.y >= current.bottom]
            row = min((region.y for _, region in below), default=None)
        else:
            below = [(card, region) for card, region in cards if region.bottom <= current.y]
            row = max((region.y for _, region in below), default=None)
        if row is None:
            if dy < 0:
                self.scroll_home(animate=False)
            else:
                self.scroll_end(animate=False)
            return
        candidates = [(card, region) for card, region in below if region.y == row]
        best = min(candidates, key=lambda item: abs(item[1].x + item[1].width / 2 - middle))
        best[0].focus()


def metric_values(data):
    """Do not replace absent readings with reassuring zeros."""
    score = data.get("health_score")
    cpu = (data.get("cpu") or {}).get("usage")
    memory = (data.get("memory") or {}).get("used_percent")
    interface = busiest_interface(data)
    if interface:
        rate = f"↓{interface['rx_rate_mbs']:.2f} ↑{interface['tx_rate_mbs']:.2f} MiB/s"
    else:
        rate = "Unavailable" if "Network" in (data.get("unavailable") or {}) else "Waiting for sample"
    batteries = data.get("batteries") or []
    if batteries:
        battery = batteries[0]
        power = f"{battery.get('percent', '—')}% · {battery.get('status', '')}"
    else:
        power = "Unavailable" if "Battery" in (data.get("unavailable") or {}) else "No internal battery"
    return {
        "resource": f"{score}/100" if score is not None else "Unavailable",
        "cpu": f"{cpu:.1f}%" if isinstance(cpu, (int, float)) else "Unavailable",
        "memory": f"{memory:.1f}%" if isinstance(memory, (int, float)) else "Unavailable",
        "network": rate,
        "power": power,
    }


def busiest_interface(data):
    network = [item for item in data.get("network") or []
               if item.get("rx_rate_mbs") is not None and item.get("tx_rate_mbs") is not None]
    return max(network, key=lambda item: item["rx_rate_mbs"] + item["tx_rate_mbs"], default=None)


def startup_disk(data):
    system = os.environ.get("SystemDrive", "C:")
    mounts = ("/", "C:\\", "C:", system, system + "\\")
    disks = data.get("disks") or []
    return next((item for item in disks if item.get("mount") in mounts), disks[0] if disks else None)


def metric_cards(data):
    """Card readings: {key: (value, caption, percent, inverted)}; absent readings stay "Unavailable"."""
    values = metric_values(data)
    cards = {}
    disk = startup_disk(data)
    if disk and disk.get("total"):
        free = disk["total"] - disk.get("used", 0)
        # "free" right after the number: narrow cards cut the end, and "60.6 of 227.7 G…" reads as used space.
        cards["disk"] = (f"{disk['used_percent']:.0f}% used", f"{size_label(free)} free of {size_label(disk['total'])}",
                         disk["used_percent"], False)
    else:
        cards["disk"] = ("Unavailable", None, None, False)
    cpu = data.get("cpu") or {}
    usage = cpu.get("usage")
    details = []
    if isinstance(cpu.get("load1"), (int, float)):
        details.append(f"load {cpu['load1']:.2f}")
    if cpu.get("core_count"):
        details.append(f"{cpu['core_count']} cores")
    cards["cpu"] = (values["cpu"], " · ".join(details) or None, usage if isinstance(usage, (int, float)) else None, False)
    memory = data.get("memory") or {}
    percent = memory.get("used_percent")
    caption = f"{size_label(memory['used'])} of {size_label(memory['total'])}" if memory.get("used") and memory.get("total") else None
    cards["memory"] = (values["memory"], caption, percent if isinstance(percent, (int, float)) else None, False)
    score = data.get("health_score")
    message = (data.get("health_score_msg") or "").split(".")[0] or None
    cards["resource"] = (values["resource"], message, score, True)
    interface = busiest_interface(data)
    caption = (f"{interface['name']} · ↓{size_label(interface.get('received', 0))} ↑{size_label(interface.get('sent', 0))}"
               if interface else None)
    cards["network"] = (values["network"], caption, None, False)
    batteries = data.get("batteries") or []
    if batteries and isinstance(batteries[0].get("percent"), (int, float)):
        battery = batteries[0]
        status = ", ".join(part for part in (battery.get("status"), battery.get("time_left")) if part)
        cards["power"] = (f"{battery['percent']}%", status or None, battery["percent"], True)
    else:
        cards["power"] = (values["power"].replace("No internal battery", "No battery"), None, None, False)
    hardware = data.get("hardware") or {}
    caption = " · ".join(str(part) for part in (hardware.get("model"), hardware.get("os_version")) if part) or None
    cards["uptime"] = (str(data.get("uptime") or "Unavailable"), caption, None, False)
    return cards
