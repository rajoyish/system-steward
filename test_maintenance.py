"""Native maintenance tests: only temporary homes and fake backend processes."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from textual.widgets import Button, DataTable, Input, Select, SelectionList

import native_maintenance as maintenance
import system_actions
from system_status import cpu_usage, network_rates, parse_battery, parse_netstat, parse_vm_stats, resource_score
from maintenance_tui import MaintenanceConfirm, MaintenanceScreen
from system_steward_tui import SystemStewardApp


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()

    def put(self, name, content="fixture"):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_cleanup_scope_and_redirected_paths(self):
        cache = self.put("Library/Caches/example/data")
        log = self.put("Library/Logs/example.log")
        protected = self.put("Library/Preferences/example.plist")
        outside = self.put("outside/data")
        (self.home / "Library/Caches/redirect").symlink_to(outside.parent)
        groups, warnings = maintenance.discover("clean", self.home)
        self.assertEqual({g.targets[0] for g in groups}, {cache.parent, log})
        self.assertTrue(warnings)
        maintenance.trash_group(next(g for g in groups if g.targets == (cache.parent,)), self.home)
        self.assertFalse(cache.exists())
        self.assertTrue(protected.exists())
        self.assertTrue(outside.exists())
        self.assertEqual(len(list((self.home / ".Trash").iterdir())), 1)

    def test_changed_target_and_trash_symlink_refused(self):
        path = self.put("Downloads/test.dmg")
        group = maintenance.candidate("Installer", [path], "test")
        path.rename(path.with_suffix(".old"))
        path.write_text("new")
        with self.assertRaisesRegex(ValueError, "changed"):
            maintenance.trash_group(group, self.home)
        self.assertTrue(path.exists())
        group = maintenance.candidate("Installer", [path], "test")
        (self.home / ".Trash").symlink_to(self.home / "Downloads")
        with self.assertRaisesRegex(ValueError, "redirected"):
            maintenance.trash_group(group, self.home)

    def test_artifacts_and_installers_leave_source_and_unrelated_builds(self):
        self.put("sites/project/package.json")
        generated = self.put("sites/project/dist/index.js")
        modules = self.put("sites/project/node_modules/dep/index.js")
        source = self.put("sites/project/src/index.js")
        unrelated = self.put("sites/photos/build/family.jpg")
        groups, _ = maintenance.discover("purge", self.home)
        self.assertEqual({g.targets[0] for g in groups}, {generated.parent, modules.parents[1]})
        installer = self.put("Downloads/release.DMG")
        kept = self.put("Downloads/notes.txt")
        linked = self.home / "Desktop"
        linked.symlink_to(self.home / "Downloads")
        groups, warnings = maintenance.discover("installer", self.home)
        self.assertEqual([g.targets[0] for g in groups], [installer])
        self.assertTrue(warnings)
        self.assertTrue(source.exists() and unrelated.exists() and kept.exists())

    def test_app_residuals_match_exact_bundle_id(self):
        app = self.home / "Applications/Example.app"
        self.put("Applications/Example.app/Contents/Info.plist", '<?xml version="1.0"?><plist version="1.0"><dict><key>CFBundleIdentifier</key><string>com.example.app</string></dict></plist>')
        residual = self.put("Library/Caches/com.example.app/cache").parent
        kept = self.put("Library/Application Support/Example/user-project")
        groups, _ = maintenance.discover("uninstall", self.home)
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0].targets), {app, residual})
        self.assertTrue(kept.exists())

    def test_status_nullable_collections_and_zero_usage(self):
        rows = maintenance.status_rows({"cpu": {"usage": 0}, "network": None, "batteries": None})
        self.assertIn(("CPU", "0.0%", 'Usage: 0'), rows)


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_errors_surface_and_output_streams(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "fake-command"
            binary.write_text('#!/bin/sh\nprintf "first line\\n"\nprintf "permission denied\\n" >&2\nexit 3\n')
            binary.chmod(0o700)
            lines = []
            with self.assertRaisesRegex(RuntimeError, "permission denied"):
                await system_actions.run_process([str(binary)], lines.append)
            self.assertIn("first line", lines)

    async def test_timeout_terminates_backend(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "fake-command"
            binary.write_text('#!/bin/sh\nsleep 30\n')
            binary.chmod(0o700)
            with self.assertRaises(asyncio.TimeoutError):
                await system_actions.run_process([str(binary)], timeout=.1)


class NativeUITests(unittest.IsolatedAsyncioTestCase):
    async def wait_for(self, pilot, condition):
        for _ in range(100):
            if condition():
                await pilot.pause(.05)
                return
            await pilot.pause(.05)
        self.fail("Timed out waiting for native maintenance UI")

    async def test_health_selection_confirmation_trash_and_resize(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            root = home / "sites"
            root.mkdir()
            cache = home / "Library/Caches/example/data"
            cache.parent.mkdir(parents=True)
            cache.write_text("cache")
            data = {"health_score": 87, "health_score_msg": "Good", "cpu": {"usage": 12}, "network": None, "batteries": None}
            with patch("maintenance_tui.SystemSampler.sample", return_value=data) as backend:
                app = SystemStewardApp(root=root, home=home, timeout=3)
                async with app.run_test(size=(140, 44)) as pilot:
                    await pilot.press("m")
                    await self.wait_for(pilot, lambda: isinstance(app.screen, MaintenanceScreen) and app.screen.last_status)
                    screen = app.screen
                    self.assertGreater(screen.query_one("#health-table", DataTable).row_count, 2)
                    screen.query_one("#maintenance-feature", Select).value = "clean"
                    await self.wait_for(pilot, lambda: screen.preview_ready and not screen.fetching)
                    choices = screen.query_one("#maintenance-list", SelectionList)
                    self.assertEqual(choices.selected, [])
                    self.assertTrue(screen.query_one("#maintenance-review", Button).disabled)
                    choices.select(0)
                    await pilot.pause()
                    await pilot.click("#maintenance-review")
                    self.assertIsInstance(app.screen, MaintenanceConfirm)
                    # Enter cancels by default; no typed phrase is needed.
                    self.assertEqual(app.focused.id, "maintenance-cancel")
                    await pilot.press("enter")
                    self.assertTrue(cache.exists())
                    await pilot.click("#maintenance-review")
                    self.assertEqual(len(app.screen.query(Input)), 0)
                    await pilot.click("#maintenance-confirm-run")
                    await self.wait_for(pilot, lambda: not cache.exists() and not screen.busy)
                    self.assertEqual(len(list((home / ".Trash").iterdir())), 1)
                    self.assertTrue(screen.query_one("#maintenance-review", Button).disabled)
                    await pilot.resize_terminal(80, 24)
                    await pilot.pause()
                    self.assertLessEqual(screen.query_one("#maintenance-actions").region.bottom, 24)
                    app.save_screenshot("maintenance-cleanup.svg", path="previews")
                    await pilot.press("escape")
                    self.assertNotIsInstance(app.screen, MaintenanceScreen)
                    self.assertGreaterEqual(backend.call_count, 2)  # Dashboard plus detailed status.

    async def test_native_analysis_and_backend_preview_gates_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            (home / "project").mkdir()
            backend = AsyncMock()
            with patch("maintenance_tui.SystemSampler.sample", return_value={"cpu": {"usage": 12}}), patch("maintenance_tui.system_actions.run_action", backend):
                app = SystemStewardApp(root=home, home=home, timeout=3)
                async with app.run_test(size=(140, 44)) as pilot:
                    await pilot.press("m")
                    await self.wait_for(pilot, lambda: isinstance(app.screen, MaintenanceScreen) and not app.screen.fetching)
                    screen = app.screen
                    screen.query_one("#maintenance-feature", Select).value = "optimize"
                    await self.wait_for(pilot, lambda: screen.preview_ready and not screen.fetching)
                    backend.assert_not_awaited()
                    selections = screen.query_one("#maintenance-list", SelectionList)
                    self.assertEqual(selections.selected, [])
                    selections.select(1)
                    await pilot.pause()
                    await pilot.click("#maintenance-review")
                    self.assertIsInstance(app.screen, MaintenanceConfirm)
                    await pilot.press("escape")
                    backend.assert_not_awaited()
                    await pilot.click("#maintenance-review")
                    self.assertEqual(len(app.screen.query(Input)), 0)
                    await pilot.click("#maintenance-confirm-run")
                    await self.wait_for(pilot, lambda: backend.await_count == 1 and not screen.busy)
                    self.assertEqual(backend.await_args.args[0], system_actions.ACTIONS[1])
                    screen.query_one("#maintenance-feature", Select).value = "analyze"
                    await self.wait_for(pilot, lambda: screen.preview_ready and not screen.fetching)
                    screen.query_one("#maintenance-path", Input).value = str(home / "project")
                    await pilot.click("#maintenance-review")
                    await self.wait_for(pilot, lambda: app.root == home / "project")
                    self.assertNotIsInstance(app.screen, MaintenanceScreen)

    async def test_touchid_native_confirmation_and_no_configuration_on_open(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            state = {"enabled": False, "legacy": False, "supported": True, "description": "Disabled", "details": "Local configuration"}
            plan = system_actions.TouchIDPlan(True, None, b"auth sufficient pam_tid.so\n", hashlib.sha256(b"sudo").hexdigest(), "Enable Touch ID")
            async def apply(reviewed, output):
                self.assertEqual(reviewed, plan)
                state["enabled"] = True
                state["description"] = "Enabled"
                output("Verified")
            with patch("maintenance_tui.SystemSampler.sample", return_value={}), patch("maintenance_tui.system_actions.touchid_status", side_effect=lambda: dict(state)), patch("maintenance_tui.system_actions.plan_touchid", return_value=plan), patch("maintenance_tui.system_actions.apply_touchid", AsyncMock(side_effect=apply)) as writer:
                app = SystemStewardApp(root=home, home=home, timeout=3)
                async with app.run_test(size=(100, 36)) as pilot:
                    await pilot.press("m")
                    await self.wait_for(pilot, lambda: isinstance(app.screen, MaintenanceScreen) and not app.screen.fetching)
                    screen = app.screen
                    screen.query_one("#maintenance-feature", Select).value = "touchid"
                    await self.wait_for(pilot, lambda: screen.kind == "touchid" and not screen.fetching)
                    writer.assert_not_awaited()
                    await pilot.click("#touchid-enable")
                    self.assertIsInstance(app.screen, MaintenanceConfirm)
                    await pilot.press("escape")
                    writer.assert_not_awaited()
                    await pilot.click("#touchid-enable")
                    self.assertEqual(len(app.screen.query(Input)), 0)
                    await pilot.click("#maintenance-confirm-run")
                    await self.wait_for(pilot, lambda: writer.await_count == 1 and not screen.busy)
                    self.assertTrue(screen.query_one("#touchid-enable", Button).disabled)
                    self.assertFalse(screen.query_one("#touchid-disable", Button).disabled)

    async def test_native_task_failure_stops_remaining_and_small_layout_fits(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            with patch("maintenance_tui.SystemSampler.sample", return_value={}), patch("maintenance_tui.system_actions.run_action", AsyncMock(side_effect=RuntimeError("Denied"))) as runner:
                app = SystemStewardApp(root=home, home=home, timeout=3)
                async with app.run_test(size=(80, 24)) as pilot:
                    await pilot.press("m")
                    await self.wait_for(pilot, lambda: isinstance(app.screen, MaintenanceScreen) and not app.screen.fetching)
                    screen = app.screen
                    screen.query_one("#maintenance-feature", Select).value = "optimize"
                    await self.wait_for(pilot, lambda: screen.preview_ready and not screen.fetching)
                    self.assertLessEqual(screen.query_one("#maintenance-actions").region.bottom, 24)
                    screen.run_worker(screen.execute_actions(system_actions.ACTIONS[:2]), group="maintenance-action")
                    await self.wait_for(pilot, lambda: runner.await_count == 1 and not screen.busy)
                    self.assertIn("Remaining tasks were not started", str(screen.query_one("#maintenance-summary").render()))
                    self.assertTrue(screen.query_one("#maintenance-review", Button).disabled)


if __name__ == "__main__":
    unittest.main()
