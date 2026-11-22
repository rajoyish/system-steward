"""Native dashboard formatting, shortcut routing and responsive interactions."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.widgets import DataTable

from dashboard import TOOLS, DashboardCard, metric_values
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
                    await self.wait_for(pilot, lambda: app.dashboard_sample is not None)
                    self.assertTrue(app.dashboard_expanded)
                    for key, _title, description in TOOLS:
                        card = app.query_one(f"#tool-{key}", DashboardCard)
                        self.assertIn(description, card.label.plain)
                        self.assertGreater(card.region.height, 0)
                    self.assertIn("23.4%", app.query_one("#metric-cpu", DashboardCard).label.plain)
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
                    sampler.assert_not_called()
                    self.assertGreater(app.query_one("#files", DataTable).region.height, 0)
                    await pilot.press("d")
                    await self.wait_for(pilot, lambda: app.dashboard_sample is not None)
                    self.assertTrue(app.dashboard_expanded)
                    sampler.side_effect = OSError("native sampling unavailable")
                    app.refresh_dashboard()
                    await self.wait_for(pilot, lambda: not app.dashboard_sampling)
                    self.assertIn("Unavailable", app.query_one("#metric-cpu", DashboardCard).label.plain)
                    self.assertIn("could not be refreshed", str(app.query_one("#dashboard-health-status").render()))
                    await pilot.press("d")
                    await pilot.pause()
                    self.assertFalse(app.dashboard_expanded)
                    self.assertGreater(app.query_one("#files", DataTable).region.height, 0)
                    await pilot.resize_terminal(140, 48)
                    await pilot.pause()
                    self.assertFalse(app.dashboard_expanded)  # Honor the user's choice.


if __name__ == "__main__":
    unittest.main()
