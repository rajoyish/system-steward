"""Textual integration tests; all deletions use temporary fake Library folders."""
import steward_test_env  # noqa: F401 - isolates the Keep Awake state folder
import asyncio
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from textual.widgets import Button, DataTable, Input, Select, SelectionList, Tabs

import adobe_cleanup
import chrome_cleanup
from cleanup_selection import cleanup_groups, selection_plan
from system_steward_tui import CleanupScreen, ConfirmDialog, InfoDialog, SystemStewardApp
from system_steward import Entry


class SelectionTests(unittest.TestCase):
    def test_cache_scope_excludes_website_data_and_selected_plan_is_exact(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            data = home / "Library/Application Support/Google/Chrome/Default"
            for name in ("Cookies", "Cookies-wal", "IndexedDB/data", "Cache/data"):
                path = data / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture")
            plan = chrome_cleanup.build_plan(home)
            caches = cleanup_groups("Chrome", plan)
            self.assertEqual(len(caches), 1)
            self.assertTrue(all(path.name == "Cache" for group in caches for path in group.targets))
            groups = cleanup_groups("Chrome", plan, "sites")
            index = next(i for i, group in enumerate(groups) if "website data" in group.label)
            restricted = selection_plan(plan, groups, {index})
            self.assertEqual(set(restricted.targets), {data / "Cookies", data / "Cookies-wal", data / "IndexedDB"})
            self.assertNotIn(data / "Cache", restricted.targets)
            for invalid in (set(), {-1}, {999}):
                with self.assertRaises(ValueError):
                    selection_plan(plan, groups, invalid)


class UITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / "projects"
        self.root.mkdir()
        (self.root / "website").mkdir()
        (self.root / "website/index.html").write_text("hello")
        (self.root / "notes.md").write_text("notes")

    def put(self, relative):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
        return path

    async def wait_for(self, pilot, condition, timeout=15):
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition() and asyncio.get_running_loop().time() < deadline:
            await pilot.pause(.05)
        self.assertTrue(condition(), "Timed out waiting for UI state")
        await pilot.pause(.05)

    def app(self):
        return SystemStewardApp(root=self.root, home=self.home, timeout=3)

    async def test_explore_tabs_filter_above_table_and_follow_folder_navigation(self):
        entries = [Entry("Browser cache", str(self.root / "notes.md"), "Browsers"),
                   Entry("Project", str(self.root / "website"), "Projects")]
        app = SystemStewardApp(home=self.home, timeout=3)
        with patch("tui.app.audit_entries", return_value=entries):
            async with app.run_test(size=(140, 40)) as pilot:
                await self.wait_for(pilot, lambda: app.scan and app.scan.finished.is_set())
                navigation = app.query_one("#navigation", Tabs)
                table = app.query_one("#files", DataTable)
                self.assertIs(navigation.parent.parent, app.query_one("#main-panel"))
                self.assertLessEqual(navigation.region.bottom, table.region.y)
                self.assertEqual(table.row_count, 2)
                await pilot.click("#Browsers")
                await self.wait_for(pilot, lambda: app.group == "Browsers" and table.row_count == 1)
                await pilot.press("right")
                await self.wait_for(pilot, lambda: app.group == "Creative" and table.row_count == 0)
                navigation.active = "Projects"
                await self.wait_for(pilot, lambda: app.group == "Projects" and table.row_count == 1)
                app.explore()
                await self.wait_for(pilot, lambda: app.root == self.root / "website" and app.scan.finished.is_set())
                self.assertEqual(navigation.active, "All")
                app.action_back()
                await self.wait_for(pilot, lambda: app.root is None and app.scan.finished.is_set())
                self.assertEqual(navigation.active, "Projects")
                await pilot.resize_terminal(80, 24)
                await pilot.pause()
                self.assertFalse(app.query_one("#sidebar").display)
                self.assertTrue(navigation.visible)
                self.assertEqual(navigation.region.height, 2)
                self.assertGreater(table.region.height, 0)
                navigation.focus()
                await pilot.press("right", "right")
                await self.wait_for(pilot, lambda: app.group == "Apps")

    async def test_scan_progress_updates_cells_without_rebuilding_the_table(self):
        entries = [Entry("Small", str(self.root / "notes.md"), "Projects"),
                   Entry("Large", str(self.root / "website"), "Projects")]
        app = SystemStewardApp(home=self.home, timeout=3)
        with patch("tui.app.audit_entries", return_value=entries):
            async with app.run_test(size=(140, 40)) as pilot:
                await self.wait_for(pilot, lambda: app.scan and app.scan.finished.is_set())
                table = app.query_one("#files", DataTable)
                await self.wait_for(pilot, lambda: table.row_count == 2)
                with patch.object(app, "populate_table", wraps=app.populate_table) as rebuild, \
                        patch.object(table, "update_cell", wraps=table.update_cell) as update:
                    # Same order, new value: one row's cells change in place.
                    order = list(app.visible_entries)
                    first = app.visible_entries[order[0]]
                    first.size += 1
                    app.tick()
                    rebuild.assert_not_called()
                    self.assertTrue(all(call.args[0] == first.path for call in update.call_args_list))
                    update.reset_mock()
                    app.tick()  # nothing changed: no work at all
                    update.assert_not_called()
                    # Order change mid-scan is deferred, then applied once the interval has passed.
                    app.scan.finished.clear()
                    app.visible_entries[order[1]].size = first.size + 10 ** 9
                    app.last_order = time.monotonic()
                    app.tick()
                    rebuild.assert_not_called()
                    self.assertTrue(app.order_stale)
                    app.last_order -= 5
                    app.tick()
                    rebuild.assert_called_once()
                    self.assertEqual(list(app.visible_entries)[0], order[1])
                    app.scan.finished.set()

    async def test_explorer_search_navigation_resize_and_export(self):
        app = self.app()
        async with app.run_test(size=(140, 44)) as pilot:
            await self.wait_for(pilot, lambda: app.scan and app.scan.finished.is_set())
            table = app.query_one("#files", DataTable)
            self.assertEqual(table.row_count, 2)
            table.move_cursor(row=table.get_row_index(str(self.root / "website")))
            await pilot.pause()
            await pilot.press("enter")
            await self.wait_for(pilot, lambda: app.root == self.root / "website" and app.scan.finished.is_set())
            await pilot.press("backspace")
            await self.wait_for(pilot, lambda: app.root == self.root and app.scan.finished.is_set())
            app.query_one("#search", Input).value = "no-match"
            await self.wait_for(pilot, lambda: table.row_count == 0)
            app.query_one("#search", Input).value = "notes"
            await self.wait_for(pilot, lambda: table.row_count == 1)
            self.assertEqual(app.active_path, str(self.root / "notes.md"))
            app.query_one("#search", Input).value = ""
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            self.assertFalse(app.query_one("#sidebar").display)
            self.assertFalse(app.query_one("#details").display)
            self.assertEqual(len(table.columns), 3)
            await pilot.resize_terminal(140, 44)
            await pilot.pause()
            self.assertEqual(len(table.columns), 4)
            with patch("tui.app.Path.cwd", return_value=self.root):
                await pilot.click("#export")
            self.assertEqual(len(list(self.root.glob("system-steward-report-*.json"))), 1)

    async def test_adobe_selection_cancel_confirmation_and_results(self):
        kept = self.put("Library/Application Support/Adobe/Common/Media Cache Files/clip.cfa")
        cleared = self.put("Library/Caches/Adobe InDesign/frames/cache")
        catalog = self.put("Library/Caches/Adobe InDesign/keep.lrcat")
        app = self.app()
        with patch("adobe_cleanup.adobe_running", return_value=False):
            async with app.run_test(size=(140, 44)) as pilot:
                await pilot.press("a")
                await self.wait_for(pilot, lambda: isinstance(app.screen, CleanupScreen) and app.screen.scan
                                    and app.screen.scan.finished.is_set() and not app.screen.query_one("#cleanup-list").disabled)
                screen = app.screen
                await pilot.press("c", "r")
                self.assertIs(app.screen, screen)
                selections = screen.query_one("#cleanup-list", SelectionList)
                self.assertEqual(selections.selected, [])
                self.assertTrue(screen.query_one("#review", Button).disabled)
                await pilot.click("#purge-guide")
                self.assertIsInstance(app.screen, InfoDialog)
                await pilot.press("escape")
                await pilot.click("#select-all")
                self.assertEqual(len(selections.selected), 2)
                await pilot.click("#select-none")
                index = next(i for i, group in enumerate(screen.groups) if group.label == "Adobe InDesign")
                selections.select(index)
                await pilot.pause()
                await pilot.click("#review")
                self.assertIsInstance(app.screen, ConfirmDialog)
                await pilot.click("#cancel-delete")
                self.assertTrue(cleared.exists())
                self.assertTrue(kept.exists())
                await pilot.click("#review")
                self.assertFalse(app.screen.query_one("#confirm-delete", Button).disabled)
                self.assertEqual(len(app.screen.query(Input)), 0)
                # Opening the dialog focuses Cancel; Enter must not delete.
                await pilot.press("enter")
                self.assertTrue(cleared.exists())
                await pilot.click("#review")
                await pilot.press("y")
                await self.wait_for(pilot, lambda: screen.completed)
                self.assertFalse(cleared.exists())
                self.assertTrue(cleared.parent.parent.is_dir())
                self.assertTrue(kept.exists())
                self.assertTrue(catalog.exists())
                self.assertFalse(screen.job.errors)
                self.assertFalse(screen.query_one("#review", Button).disabled)
                await pilot.click("#review")
                self.assertIsInstance(app.screen, InfoDialog)
                await pilot.press("escape")
                await pilot.click("#cleanup-back")
                self.assertNotIsInstance(app.screen, CleanupScreen)

    async def test_chrome_scope_running_app_block_and_small_confirmation(self):
        cookie = self.put("Library/Application Support/Google/Chrome/Default/Cookies")
        self.put("Library/Application Support/Google/Chrome/Default/Cookies-wal")
        self.put("Library/Caches/Google/Chrome/Default/Cache/data")
        app = self.app()
        with patch("chrome_cleanup.chrome_running", return_value=True):
            async with app.run_test(size=(80, 28)) as pilot:
                await pilot.press("c")
                await self.wait_for(pilot, lambda: app.screen.scan and app.screen.scan.finished.is_set()
                                    and not app.screen.query_one("#cleanup-list").disabled)
                screen = app.screen
                self.assertFalse(any("website data" in group.label for group in screen.groups))
                screen.query_one("#cleanup-scope", Select).value = "sites"
                await self.wait_for(pilot, lambda: any("website data" in group.label for group in screen.groups)
                                    and screen.scan.finished.is_set() and not screen.query_one("#cleanup-list").disabled)
                screen.query_one("#cleanup-list", SelectionList).select_all()
                await pilot.pause()
                await pilot.click("#review")
                self.assertIsInstance(app.screen, ConfirmDialog)
                await pilot.resize_terminal(64, 24)
                await pilot.pause()
                button = app.screen.query_one("#confirm-delete", Button)
                self.assertFalse(button.disabled)
                self.assertLessEqual(button.region.bottom, 24)
                await pilot.click("#confirm-delete")
                await self.wait_for(pilot, lambda: not app.screen.checking)
                self.assertIsInstance(app.screen, ConfirmDialog)
                self.assertIn("still running", str(app.screen.query_one("#confirm-status").render()))
                self.assertTrue(cookie.exists())
                self.assertIsNone(screen.job)
                await pilot.resize_terminal(60, 20)
                await pilot.pause()
                self.assertTrue(button.disabled)
                await pilot.press("escape")


if __name__ == "__main__":
    unittest.main()
