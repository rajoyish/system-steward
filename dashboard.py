"""Dashboard cards for native resource readings and maintenance shortcuts."""
from rich.text import Text
from textual.widgets import Button

from system_steward import clean

TOOLS = (
    ("clean", "Clean system", "Select app caches and logs to clear."),
    ("purge", "Developer artifacts", "Find dependencies and build output."),
    ("installer", "Clean installers", "Find DMG, PKG and ISO downloads."),
    ("uninstall", "Uninstall apps", "Review apps and their leftover files."),
    ("optimize", "System maintenance", "Repair DNS, previews, apps or search."),
    ("touchid", "Touch ID for sudo", "Configure fingerprint access for sudo."),
)

METRICS = (
    ("resource", "RESOURCE ESTIMATE", "CPU, RAM & disk headroom", "An app-defined resource estimate, not a hardware diagnosis. Open system status for the calculation and readings."),
    ("cpu", "CPU LOAD", "Processor activity", "Percentage of CPU time spent working, measured between native system samples."),
    ("memory", "MEMORY", "Active, wired & compressed", "Physical RAM used by active pages, wired pages and the compressor. Inactive cache is excluded."),
    ("network", "NETWORK", "Download / upload rate", "Incoming and outgoing data rates on the busiest measured interface. The first reading waits for the next sample."),
    ("power", "POWER", "Battery charge & status", "Battery percentage and charging state. Desktop Macs may have no internal battery."),
)


class DashboardCard(Button):
    """A keyboard- and mouse-accessible card with a plain-language description."""
    def __init__(self, title, description, *, id, classes="", tooltip=None):
        self.card_title, self.description = title, description
        super().__init__(id=id, classes=classes, tooltip=tooltip or description)
        self.set_value(None)

    def set_value(self, value):
        label = Text(self.card_title, style="bold #b7c8d6")
        if value is not None:
            label.append("\n" + clean(value), style="bold #71e5bb")
        label.append("\n" + self.description, style="#8395a5")
        self.label = label


def metric_values(data):
    """Do not replace absent readings with reassuring zeros."""
    score = data.get("health_score")
    cpu = (data.get("cpu") or {}).get("usage")
    memory = (data.get("memory") or {}).get("used_percent")
    network = [item for item in data.get("network") or []
               if item.get("rx_rate_mbs") is not None and item.get("tx_rate_mbs") is not None]
    interface = max(network, key=lambda item: item["rx_rate_mbs"] + item["tx_rate_mbs"], default=None)
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
