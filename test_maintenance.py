"""Native maintenance tests: only temporary homes and fake backend processes."""
import asyncio
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from textual.widgets import Button, DataTable, Input, Select, SelectionList

from maintenance_tui import MaintenanceConfirm, MaintenanceScreen
import native_maintenance as maintenance
import steward_test_env  # noqa: F401 - isolates the Keep Awake state folder
import system_actions
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
        maintenance.trash_group(next(g for g in groups if g.targets == (cache.parent,)), self.home, backend="folder")
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
            maintenance.trash_group(group, self.home, backend="folder")
        self.assertTrue(path.exists())
        group = maintenance.candidate("Installer", [path], "test")
        (self.home / ".Trash").symlink_to(self.home / "Downloads")
        with self.assertRaisesRegex(ValueError, "redirected"):
            maintenance.trash_group(group, self.home, backend="folder")

    def test_recycle_backend_receives_only_verified_targets(self):
        first, second = self.put("Downloads/a.msi"), self.put("Downloads/b.exe")
        group = maintenance.candidate("Installers", [first, second], "test")
        recycled = []
        moved = maintenance.trash_group(group, self.home, backend=lambda p: (recycled.append(p), p.unlink()))
        self.assertEqual(recycled, [first, second])
        self.assertEqual(moved, [(first, maintenance.RECYCLE_BIN), (second, maintenance.RECYCLE_BIN)])
        self.assertFalse((self.home / ".Trash").exists())
        # A target swapped after preview is refused before anything is recycled.
        third = self.put("Downloads/c.iso")
        group = maintenance.candidate("Installer", [third], "test")
        third.rename(third.with_suffix(".old"))
        third.write_text("replacement")
        recycled.clear()
        with self.assertRaisesRegex(ValueError, "changed"):
            maintenance.trash_group(group, self.home, backend=recycled.append)
        self.assertEqual(recycled, [])

    def test_recycle_path_refuses_relative_and_wildcard_paths(self):
        for path in ("relative/file.txt", str(self.home / "*.dmg")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                maintenance.recycle_path(path)

    @unittest.skipUnless(sys.platform == "win32", "Windows Recycle Bin")
    def test_recycle_path_refuses_drives_without_a_recycle_bin(self):
        from platforms import windows as win
        with patch("platforms.windows.drive_type", return_value=win.DRIVE_REMOTE), \
                patch.object(win.api().shell32, "SHFileOperationW") as shell:
            with self.assertRaisesRegex(ValueError, "unavailable"):
                maintenance.recycle_path(self.put("Downloads/x.msi"))
            shell.assert_not_called()

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
    async def wait_for(self, pilot, condition, timeout=15):
        # Wall-clock deadline: background workers run slower while the whole suite is busy.
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
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
            def fake_recycle(path):
                (home / ".Trash").mkdir(exist_ok=True)
                shutil.move(str(path), str(home / ".Trash" / path.name))
            with patch("maintenance_tui.SystemSampler.sample", return_value=data) as backend, \
                    patch("native_maintenance.recycle_path", side_effect=fake_recycle):
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
                    app.save_screenshot("maintenance-cleanup.svg", path=str(Path("previews").resolve()))
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

    def test_macos_only_tools_are_not_offered_on_windows(self):
        windows = {key for _, key in maintenance.features("win32")}
        mac = {key for _, key in maintenance.features("darwin")}
        self.assertTrue(maintenance.MACOS_ONLY_FEATURES <= mac)
        self.assertFalse(maintenance.MACOS_ONLY_FEATURES & windows)
        self.assertEqual(mac - windows, maintenance.MACOS_ONLY_FEATURES)
        from dashboard import tools
        for platform in ("win32", "darwin"):
            cards = {key for key, *_ in tools(platform)}
            with self.subTest(platform=platform):
                self.assertEqual(len(cards), 6)  # two rows of three
                offered = {key for _, key in maintenance.features(platform)}
                self.assertTrue(cards <= offered | {"status", "awake"})

    def test_cleanup_wording_matches_the_platform(self):
        from strings import for_platform
        windows, mac = for_platform("win32"), for_platform("darwin")
        self.assertEqual(windows.trash_title, "Recycle Bin")
        self.assertEqual(mac.trash_title, "Trash")
        self.assertNotIn("⌘", windows.quit_app)
        self.assertIn(".msi", windows.installer_types)
        self.assertIn(".dmg", mac.installer_types)
        for field in ("trash", "empty_trash", "clean_note", "admin_prompt", "keep_awake_intro", "sampled_from"):
            with self.subTest(field=field):
                self.assertNotIn("macOS", getattr(windows, field))
                self.assertNotIn("Windows", getattr(mac, field))

    @unittest.skipUnless(sys.platform == "darwin", "Touch ID is a macOS-only tool")
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
