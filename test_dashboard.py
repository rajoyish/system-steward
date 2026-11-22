"""Native dashboard formatting, shortcut routing and responsive interactions."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.widgets import DataTable

from dashboard import TOOLS, DashboardCard, metric_cards, metric_values
from maintenance_tui import MaintenanceScreen
import steward_test_env  # noqa: F401 - isolates the Keep Awake state folder
from system_steward_tui import SystemStewardApp

SAMPLE = {"health_score": 85, "cpu": {"usage": 23.4}, "memory": {"used_percent": 56.2},
          "network": [{"name": "en0", "rx_rate_mbs": 1.25, "tx_rate_mbs": .15}], "batteries": []}


class MetricCardTests(unittest.TestCase):
    def test_missing_zero_and_interface_rates_have_distinct_meanings(self):
        absent = metric_values({"unavailable": {"Network": "denied", "Battery": "denied"}})
        self.assertEqual(absent["cpu"], "Unavailable")
        self.assertEqual(absent["network"], "Unavailable")
        self.assertEqual(absent["power"], "Unavailable")
        self.assertEqual(metric_values({"cpu": {"usage": 0}})["cpu"], "0.0%")
        self.assertEqual(metric_values({})["network"], "Waiting for sample")
        sample = {**SAMPLE, "network": [*SAMPLE["network"], {"name": "utun0", "rx_rate_mbs": .5, "tx_rate_mbs": .1}]}
        self.assertEqual(metric_values(sample)["network"], "↓1.25 ↑0.15 MiB/s")
        self.assertEqual(metric_values(SAMPLE)["power"], "No internal battery")

    def test_cards_add_context_without_inventing_readings(self):
        sample = {**SAMPLE, "cpu": {"usage": 23.4, "load1": 1.5, "core_count": 8},
                  "memory": {"used_percent": 50.0, "used": 8 * 2**30, "total": 16 * 2**30},
                  "disks": [{"mount": "/", "used": 75 * 2**30, "total": 100 * 2**30, "used_percent": 75.0}],
                  "batteries": [{"percent": 40, "status": "discharging", "time_left": "2:10 remaining"}],
                  "uptime": "1d 2h 3m"}
        cards = metric_cards(sample)
        self.assertEqual(cards["disk"][:3], ("75% used", "25.0 of 100.0 GiB free", 75.0))
        self.assertEqual(cards["cpu"][1], "load 1.50 · 8 cores")
        self.assertEqual(cards["memory"][1], "8.0 GiB of 16.0 GiB")
        self.assertEqual(cards["power"][:3], ("40%", "discharging, 2:10 remaining", 40))
        self.assertEqual(cards["uptime"][0], "1d 2h 3m")
        empty = metric_cards({})
        self.assertEqual(empty["disk"][0], "Unavailable")
        self.assertEqual(empty["uptime"][0], "Unavailable")
        self.assertIsNone(empty["cpu"][2])


class DashboardUITests(unittest.IsolatedAsyncioTestCase):
    async def wait_for(self, pilot, condition, timeout=15):
        # Wall-clock deadline: background workers run slower while the whole suite is busy.
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if condition():
                await pilot.pause(.05)
                return
            await pilot.pause(.05)
        self.fail("Timed out waiting for dashboard state")

    async def test_described_cards_route_to_preview_and_metrics_open_status(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            installer = home / "Downloads/example.dmg"
            installer.parent.mkdir()
            installer.write_text("fixture")
            with patch("tui.app.SystemSampler.sample", return_value=SAMPLE):
                app = SystemStewardApp(root=home, home=home, timeout=3)
                async with app.run_test(size=(140, 48)) as pilot:
                    self.assertEqual(app.view, "storage")  # a folder was given: start in storage
                    await pilot.press("1")
                    await self.wait_for(pilot, lambda: app.dashboard_sample is not None)
                    self.assertTrue(app.dashboard_expanded)
                    for key, _title, description in TOOLS:
                        card = app.query_one(f"#tool-{key}", DashboardCard)
                        self.assertIn(description, card.label.plain)
                        self.assertGreater(card.region.height, 0)
                    self.assertIn("23.4%", app.query_one("#metric-cpu", DashboardCard).label.plain)
                    # Arrow keys move focus between cards; Enter opens the focused one.
                    app.query_one("#metric-disk").focus()
                    await pilot.press("right")
                    self.assertEqual(app.focused.id, "metric-cpu")
                    await pilot.press("down")
                    self.assertEqual(app.focused.id, "metric-power")
                    await pilot.press("up", "left")
                    self.assertEqual(app.focused.id, "metric-disk")
                    await pilot.click("#tool-installer")
                    await self.wait_for(pilot, lambda: isinstance(app.screen, MaintenanceScreen) and app.screen.preview_ready)
                    self.assertEqual(app.screen.kind, "installer")
                    self.assertEqual(len(app.screen.groups), 1)
                    self.assertTrue(installer.exists())
                    await pilot.press("escape")
                    await pilot.pause()
                    await pilot.click("#metric-cpu")
                    await self.wait_for(pilot, lambda: isinstance(app.screen, MaintenanceScreen) and app.screen.last_status)
                    self.assertEqual(app.screen.kind, "status")
                    self.assertTrue(installer.exists())

    async def test_compact_toggle_and_sampling_failure_leave_explorer_accessible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "notes.txt").write_text("fixture")
            with patch("tui.app.SystemSampler.sample", return_value=SAMPLE) as sampler:
                app = SystemStewardApp(root=root, home=root, timeout=3)
                async with app.run_test(size=(80, 24)) as pilot:
                    await self.wait_for(pilot, lambda: app.scan and app.scan.finished.is_set())
                    self.assertFalse(app.dashboard_expanded)
                    sampler.assert_not_called()  # sampling only runs while the dashboard is shown
                    self.assertGreater(app.query_one("#files", DataTable).region.height, 0)
                    await pilot.press("d")
                    await self.wait_for(pilot, lambda: app.dashboard_sample is not None)
                    self.assertTrue(app.dashboard_expanded)
                    sampler.side_effect = OSError("native sampling unavailable")
                    app.refresh_dashboard()
                    await self.wait_for(pilot, lambda: not app.dashboard_sampling)
                    self.assertIn("Unavailable", app.query_one("#metric-cpu", DashboardCard).label.plain)
                    self.assertIn("could not be refreshed", str(app.query_one("#dashboard-health-status").render()))
                    self.assertGreater(app.query_one("#metric-cpu").region.height, 0)
                    await pilot.press("d")
                    await pilot.pause()
                    self.assertFalse(app.dashboard_expanded)
                    self.assertGreater(app.query_one("#files", DataTable).region.height, 0)
                    await pilot.resize_terminal(140, 48)
                    await pilot.pause()
                    self.assertFalse(app.dashboard_expanded)  # Resizing never switches the view.
                    await pilot.click("#view-dashboard")
                    await pilot.pause()
                    self.assertTrue(app.dashboard_expanded)


if __name__ == "__main__":
    unittest.main()
