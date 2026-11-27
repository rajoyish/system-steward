"""Update check tests: no network (fetches are injected) and a throwaway state folder."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_identity import VERSION
import steward_test_env  # noqa: F401 - isolates the state folder
import updater


def bump(version, step=1):
    parts = [int(part) for part in version.split(".")]
    parts[-1] += step
    return ".".join(map(str, parts))


NEXT = bump(VERSION)


def channel(name, root=None):
    return updater.Channel(name, {"scoop": "Scoop", "winget": "winget", "brew": "Homebrew"}[name],
                           Path(root or tempfile.gettempdir()))


def serving(version):
    """A fake fetch that serves ``version`` from every channel's manifest."""
    def get(url):
        if url == updater.SCOOP_MANIFEST_URL:
            return json.dumps({"version": version})
        if url == updater.BREW_FORMULA_URL:
            return f'url "https://github.com/x/y/releases/download/v{version}/system-steward-{version}.tar.gz"'
        if url == updater.WINGET_VERSIONS_URL:
            return json.dumps([{"name": "3.0.1", "type": "dir"}, {"name": version, "type": "dir"},
                               {"name": "README.md", "type": "file"}])
        raise AssertionError(url)
    return get


class StateTestCase(unittest.TestCase):
    def setUp(self):
        updater.state_path().unlink(missing_ok=True)

    tearDown = setUp


class VersionTests(unittest.TestCase):
    def test_numeric_comparison(self):
        self.assertTrue(updater.newer("3.0.10", "3.0.9"))
        self.assertTrue(updater.newer("v3.1", "3.0.9"))
        self.assertFalse(updater.newer("3.0", "3.0.0"))
        self.assertFalse(updater.newer("3.0.1", "3.0.2"))
        self.assertFalse(updater.newer("garbage", "3.0.0"))


class ChannelTests(unittest.TestCase):
    def test_detects_each_packaged_layout_and_ignores_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertIsNone(updater.detect_channel(root))
            (root / "scoop-manifest.json").write_text("{}")
            self.assertEqual(updater.detect_channel(root).name, "scoop")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "unins000.exe").write_bytes(b"")
            self.assertIsNone(updater.detect_channel(root))  # an uninstaller alone is not our installer
            (root / "system-steward.cmd").write_text("")
            self.assertEqual(updater.detect_channel(root).name, "winget")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "Cellar/system-steward/3.0.2/libexec"
            root.mkdir(parents=True)
            self.assertEqual(updater.detect_channel(root).name, "brew")

    def test_source_checkout_has_no_channel(self):
        self.assertIsNone(updater.detect_channel())

    def test_env_turns_checks_off(self):
        with patch.dict("os.environ", {"SYSTEM_STEWARD_NO_UPDATE_CHECK": "1"}):
            self.assertFalse(updater.checks_enabled())
        with patch.dict("os.environ", {"SYSTEM_STEWARD_NO_UPDATE_CHECK": ""}):
            self.assertTrue(updater.checks_enabled())


class CheckTests(StateTestCase):
    def test_each_channel_reads_its_own_manifest(self):
        for name in ("scoop", "brew", "winget"):
            self.assertEqual(updater.latest_for(channel(name), serving(NEXT)), NEXT, name)

    def test_offers_newer_and_caches_for_a_day(self):
        calls = []

        def get(url):
            calls.append(url)
            return serving(NEXT)(url)
        update = updater.check(channel("scoop"), now=1000, get=get)
        self.assertEqual((update.current, update.latest), (VERSION, NEXT))
        self.assertIsNotNone(updater.check(channel("scoop"), now=1000 + 3600, get=get))
        self.assertEqual(len(calls), 1)
        updater.check(channel("scoop"), now=1000 + updater.CHECK_INTERVAL + 1, get=get)
        self.assertEqual(len(calls), 2)

    def test_same_or_older_is_not_offered(self):
        self.assertIsNone(updater.check(channel("scoop"), get=serving(VERSION)))
        updater.state_path().unlink()
        self.assertIsNone(updater.check(channel("brew"), get=serving("0.1.0")))

    def test_network_failure_is_silent(self):
        def get(url):
            raise OSError("offline")
        self.assertIsNone(updater.check(channel("winget"), get=get))

    def test_skipped_and_recently_attempted_versions_wait(self):
        updater.skip_version(NEXT)
        self.assertIsNone(updater.check(channel("scoop"), now=10, get=serving(NEXT)))
        self.assertIsNotNone(updater.check(channel("scoop"), now=10, force=True, get=serving(NEXT)))
        updater.state_path().unlink()
        with patch("updater.time.time", return_value=50):
            updater.record_attempt(NEXT)
        self.assertIsNone(updater.check(channel("scoop"), now=60, get=serving(NEXT)))
        self.assertIsNotNone(updater.check(channel("scoop"), now=60 + updater.RETRY_INTERVAL, get=serving(NEXT)))
        later = bump(NEXT)
        self.assertEqual(updater.check(channel("scoop"), now=61, force=True, get=serving(later)).latest, later)


class ScriptTests(unittest.TestCase):
    def test_scoop_script_updates_buckets_then_app_then_relaunches(self):
        root = Path(r"C:\Users\Me\scoop\apps\system-steward\current")
        update = updater.Update(channel("scoop", root), VERSION, NEXT)
        with patch("updater.shutil.which", return_value=r"C:\Users\Me\scoop\shims\scoop.cmd"):
            script = updater.windows_script(update, ["C:\\100% done", 'bad"arg'])
        lines = script.split("\r\n")
        self.assertIn(r'call "C:\Users\Me\scoop\shims\scoop.cmd" "update"', lines)
        self.assertIn(r'call "C:\Users\Me\scoop\shims\scoop.cmd" "update" "system-steward"', lines)
        self.assertLess(lines.index(r'call "C:\Users\Me\scoop\shims\scoop.cmd" "update"'),
                        lines.index(r'call "C:\Users\Me\scoop\shims\scoop.cmd" "update" "system-steward"'))
        self.assertEqual(lines[-2], f'call "{root}\\system-steward.cmd" "C:\\100%% done"')

    def test_winget_script_upgrades_by_exact_id(self):
        update = updater.Update(channel("winget", r"C:\Apps\System Steward"), VERSION, NEXT)
        with patch("updater.shutil.which", return_value="winget.exe"):
            script = updater.windows_script(update, [])
        self.assertIn(f'call "winget.exe" "upgrade" "--id" "{updater.WINGET_ID}" "--exact" "--source" "winget"', script)
        self.assertTrue(script.rstrip().endswith('call "C:\\Apps\\System Steward\\system-steward.cmd"'))

    def test_windows_apply_writes_script_and_requests_launcher_update(self):
        update = updater.Update(channel("scoop"), VERSION, NEXT)
        with tempfile.TemporaryDirectory() as folder, \
                patch("updater.sys.platform", "win32"), \
                patch("updater.tempfile.gettempdir", return_value=folder), \
                patch("updater.stop_keep_awake_for_update", return_value=False), \
                patch("updater.record_attempt") as attempt:
            self.assertEqual(updater.apply(update, []), updater.UPDATE_EXIT)
            self.assertTrue((Path(folder) / updater.SCRIPT_NAME).is_file())
            attempt.assert_called_once_with(NEXT)

    def test_launcher_handles_update_exit_code(self):
        build = (Path(__file__).parent / "packaging/windows/build.ps1").read_text()
        self.assertIn(f"if %ERRORLEVEL% equ {updater.UPDATE_EXIT} if exist \"%TEMP%\\{updater.SCRIPT_NAME}\"", build)
        self.assertIn('& exit /b)', build)


class DialogTests(StateTestCase):
    def test_update_now_quits_and_leaves_pending_update(self):
        from system_steward_tui import SystemStewardApp
        from tui.dialogs import UpdateDialog
        update = updater.Update(channel("scoop"), VERSION, NEXT)

        async def scenario():
            app = SystemStewardApp(root=Path(tempfile.gettempdir()), check_updates=False)
            async with app.run_test(size=(120, 40)) as pilot:
                app.offer_update(update)
                await pilot.pause()
                self.assertIsInstance(app.screen, UpdateDialog)
                await pilot.press("u")
                await pilot.pause()
            return app
        app = asyncio.run(scenario())
        self.assertIs(app.pending_update, update)

    def test_skip_records_version(self):
        from system_steward_tui import SystemStewardApp
        update = updater.Update(channel("scoop"), VERSION, NEXT)

        async def scenario():
            app = SystemStewardApp(root=Path(tempfile.gettempdir()), check_updates=False)
            async with app.run_test(size=(120, 40)) as pilot:
                app.offer_update(update)
                await pilot.pause()
                await pilot.click("#update-skip")
                await pilot.pause()
                self.assertIsNone(app.pending_update)
                self.assertEqual(len(app.screen_stack), 1)
        asyncio.run(scenario())
        self.assertEqual(updater.load_state().get("skipped"), NEXT)


if __name__ == "__main__":
    unittest.main()
