"""Keep Awake validation, subprocess ownership, and native UI lifecycles."""
import asyncio
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from textual.widgets import Button, Checkbox, Input, Select

from keep_awake import AwakeSession, plan_session
from keep_awake_tui import KeepAwakeScreen
from maintenance_tui import MaintenanceConfirm, MaintenanceScreen
from system_steward_tui import SystemStewardApp


REAL_POPEN = subprocess.Popen


def sleeping_task(argv, **kwargs):
    """Exercise real child cleanup without creating any macOS assertions."""
    return REAL_POPEN([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)


class PlanTests(unittest.TestCase):
    def test_modes_flags_and_quoted_arguments(self):
        self.assertEqual(plan_session().argv, ("/usr/bin/caffeinate", "-i"))
        timed = plan_session(("d", "i", "m"), duration="3600")
        self.assertEqual(timed.argv, ("/usr/bin/caffeinate", "-idm", "-t", "3600"))
        watched = plan_session(mode="process", pid="12345", duration="60")
        self.assertEqual(watched.argv[-4:], ("-t", "60", "-w", "12345"))
        with tempfile.TemporaryDirectory(prefix="awake folder ") as folder:
            task = plan_session(mode="command", command='echo "a b" "$(touch unwanted)"', cwd=folder)
            self.assertEqual(task.argv[-3:], ("echo", "a b", "$(touch unwanted)"))
            self.assertEqual(task.cwd, Path(folder).resolve())

    def test_invalid_requests_never_reach_subprocess(self):
        requests = [dict(flags=()), dict(flags=("s",)), dict(mode="unknown"),
            dict(mode="command", command=""), dict(mode="command", command='echo "'),
            dict(mode="command", command="echo yes", duration="10"),
            dict(mode="command", command="-t 3"), dict(mode="command", command="echo \x00")]
        requests += [dict(duration=value) for value in ("0", "-1", "1.5", "inf", "2147483648", "１２")]
        requests += [dict(mode="process", pid=value) for value in ("", "0", "-1", "3.5")]
        with patch("keep_awake.subprocess.Popen") as spawn:
            for request in requests:
                with self.subTest(request=request), self.assertRaises(ValueError):
                    plan_session(**request)
            spawn.assert_not_called()


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.session = AwakeSession()
        self.addCleanup(self.session.stop)
        self.support = patch("keep_awake.available", return_value=True)
        self.support.start()
        self.addCleanup(self.support.stop)

    def test_only_owned_session_stops_and_countdown_is_reported(self):
        watched = REAL_POPEN([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with patch("keep_awake.subprocess.Popen", side_effect=sleeping_task) as spawn:
                plan = plan_session(mode="process", pid=str(watched.pid), duration="3600")
                self.session.start(plan)
                self.assertTrue(self.session.running)
                self.assertGreater(self.session.remaining, 3595)
                with self.assertRaises(RuntimeError):
                    self.session.start(plan)
                spawn.assert_called_once_with(plan.argv, cwd=plan.cwd, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
                self.session.stop()
            self.assertFalse(self.session.running)
            self.assertIn("stopped", self.session.status())
            self.assertIsNone(watched.poll())
            self.session.stop()  # Repeated cleanup is harmless.
        finally:
            watched.terminate()
            watched.wait(timeout=5)

    def test_unavailable_platform_missing_pid_and_spawn_errors(self):
        with patch("keep_awake.subprocess.Popen") as spawn:
            with patch("keep_awake.available", return_value=False), self.assertRaises(RuntimeError):
                self.session.start(plan_session())
            with patch("keep_awake.os.kill", side_effect=ProcessLookupError), self.assertRaises(ValueError):
                self.session.start(plan_session(mode="process", pid="12345"))
            spawn.assert_not_called()
            spawn.side_effect = OSError("launch failed")
            with self.assertRaises(OSError):
                self.session.start(plan_session())
            self.assertFalse(self.session.running)

    def test_output_is_drained_bounded_and_nonzero_exit_is_visible(self):
        def task(argv, **kwargs):
            return REAL_POPEN([sys.executable, "-c", "import sys; print('x'*200000); print('END'); sys.exit(7)"], **kwargs)
        with patch("keep_awake.subprocess.Popen", side_effect=task):
            self.session.start(plan_session(mode="command", command="example"))
        self.session.process.wait(timeout=5)
        deadline = time.monotonic() + 2
        while "END" not in self.session.output_tail() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertLessEqual(len(self.session.output_tail()), 65536)
        self.assertIn("END", self.session.output_tail())
        self.assertIn("exit code 7", self.session.status())

    def test_stop_kills_a_command_that_ignores_termination(self):
        def task(argv, **kwargs):
            return REAL_POPEN([sys.executable, "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('READY',flush=True); time.sleep(30)"], **kwargs)
        with patch("keep_awake.subprocess.Popen", side_effect=task):
            self.session.start(plan_session(mode="command", command="example"))
        deadline = time.monotonic() + 3
        while "READY" not in self.session.output_tail() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIn("READY", self.session.output_tail())
        self.session.stop()
        if sys.platform == "win32":
            self.assertIn(self.session.process.returncode, (1, 15))
        else:
            self.assertEqual(self.session.process.returncode, -signal.SIGKILL)

    def test_command_receives_literal_arguments_and_working_folder(self):
        with tempfile.TemporaryDirectory(prefix="awake cwd ") as folder:
            # Strip caffeinate only; execute the reviewed utility and arguments.
            def task(argv, **kwargs):
                return REAL_POPEN(argv[2:], **kwargs)
            exe = sys.executable.replace("\\", "/")
            command = f'{exe} -c "import os, sys; print(os.getcwd()); print(sys.argv[1])" "$(touch unwanted)"'
            with patch("keep_awake.subprocess.Popen", side_effect=task):
                self.session.start(plan_session(mode="command", command=command, cwd=folder))
            self.session.process.wait(timeout=5)
            deadline = time.monotonic() + 2
            while "unwanted" not in self.session.output_tail() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIn(str(Path(folder).resolve()), self.session.output_tail())
            self.assertIn("$(touch unwanted)", self.session.output_tail())
            self.assertFalse((Path(folder) / "unwanted").exists())
            self.assertIn("finished", self.session.status())


@unittest.skipUnless(sys.platform == "win32", "Windows specific caffeinate helper tests")
class WindowsCaffeinateTests(unittest.TestCase):
    def test_windows_caffeinate_command_execution(self):
        helper = Path(__file__).parent / "windows_caffeinate.py"
        proc = REAL_POPEN([sys.executable, str(helper), "-i", sys.executable, "-c", "import sys; sys.exit(42)"])
        code = proc.wait(timeout=5)
        self.assertEqual(code, 42)

    def test_windows_caffeinate_timed_execution(self):
        helper = Path(__file__).parent / "windows_caffeinate.py"
        start = time.monotonic()
        proc = REAL_POPEN([sys.executable, str(helper), "-id", "-t", "0.5"])
        code = proc.wait(timeout=5)
        elapsed = time.monotonic() - start
        self.assertEqual(code, 0)
        self.assertGreaterEqual(elapsed, 0.4)

    def test_windows_caffeinate_memory_trim_and_mb(self):
        import windows_caffeinate
        import os
        windows_caffeinate.trim_memory()
        mem = windows_caffeinate.get_process_memory_mb(os.getpid())
        if mem is not None:
            self.assertGreater(mem, 0)

    def test_background_session_persisted_and_restored(self):
        watched = REAL_POPEN([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with tempfile.TemporaryDirectory() as td:
                state_file = Path(td) / "awake_session.json"
                with patch("keep_awake.get_state_file", return_value=state_file):
                    from keep_awake import save_persisted_state, read_persisted_state
                    save_persisted_state({
                        "pid": watched.pid,
                        "started": time.time(),
                        "mode": "manual",
                        "duration": None,
                        "display": False,
                        "background": True
                    })
                    data = read_persisted_state()
                    self.assertIsNotNone(data)
                    self.assertEqual(data["pid"], watched.pid)

                    session = AwakeSession()
                    self.assertTrue(session.running)
                    self.assertTrue(session.background)
                    self.assertIn("background", session.status())
                    session.stop()
                    self.assertFalse(session.running)
                    self.assertFalse(state_file.exists())
        finally:
            watched.terminate()
            watched.wait(timeout=5)


class AwakeUITests(unittest.IsolatedAsyncioTestCase):
    async def wait_for(self, pilot, predicate):
        for _ in range(60):
            if predicate():
                return
            await pilot.pause(.05)
        self.fail("Timed out waiting for Keep Awake state")

    async def click(self, pilot, screen, selector):
        screen.query_one(selector).scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click(selector)
        await pilot.pause()

    async def test_confirmation_background_navigation_and_app_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = SystemStewardApp(root=root, home=root, timeout=3)
            with patch("keep_awake.available", return_value=True), patch("keep_awake_tui.available", return_value=True), \
                    patch("keep_awake.subprocess.Popen", side_effect=sleeping_task):
                async with app.run_test(size=(140, 48)) as pilot:
                    await pilot.press("k")
                    await pilot.pause()
                    screen = app.screen
                    self.assertIsInstance(screen, KeepAwakeScreen)
                    screen.query_one("#awake-display", Checkbox).value = True
                    screen.query_one("#awake-duration", Input).value = "3600"
                    await self.click(pilot, screen, "#awake-start")
                    self.assertIsInstance(app.screen, MaintenanceConfirm)
                    self.assertFalse(app.awake.running)
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertFalse(app.awake.running)
                    await self.click(pilot, screen, "#awake-start")
                    await pilot.press("y")
                    await self.wait_for(pilot, lambda: app.awake.running)
                    self.assertEqual(app.awake.plan.argv[1], "-id")
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertEqual(len(app.screen_stack), 1)
                    self.assertTrue(app.awake.running)
                    app.refresh_awake_indicator()
                    self.assertIn("on", str(app.query_one("#keep-awake", Button).label))
                    await pilot.press("k")
                    await pilot.pause()
                    self.assertTrue(app.screen.query_one("#awake-display", Checkbox).value)
                    self.assertEqual(app.screen.query_one("#awake-duration", Input).value, "3600")
                    await self.click(pilot, app.screen, "#awake-stop")
                    await self.wait_for(pilot, lambda: not app.awake.running)
                    # A subsequent session is also cleaned up by app unmount.
                    app.awake.start(plan_session())
                self.assertFalse(app.awake.running)

    async def test_menu_command_stop_confirmation_and_small_layouts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = SystemStewardApp(root=root, home=root, timeout=3)
            with patch("keep_awake.available", return_value=True), patch("keep_awake_tui.available", return_value=True), \
                    patch("keep_awake.subprocess.Popen", side_effect=sleeping_task), \
                    patch("maintenance_tui.SystemSampler.sample", return_value={}):
                async with app.run_test(size=(140, 48)) as pilot:
                    await pilot.press("m")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, MaintenanceScreen)
                    app.screen.query_one("#maintenance-feature", Select).value = "awake"
                    await self.wait_for(pilot, lambda: isinstance(app.screen, KeepAwakeScreen))
                    screen = app.screen
                    screen.query_one("#awake-duration", Input).value = "3600"
                    screen.query_one("#awake-mode", Select).value = "command"
                    screen.query_one("#awake-command", Input).value = 'echo "a b"'
                    await pilot.pause()
                    self.assertFalse(screen.query_one("#awake-duration").display)
                    await self.click(pilot, screen, "#awake-start")
                    await pilot.press("y")
                    await self.wait_for(pilot, lambda: app.awake.running)
                    self.assertNotIn("-t", app.awake.plan.argv)
                    for size in ((80, 24), (64, 24)):
                        await pilot.resize_terminal(*size)
                        await pilot.pause()
                        screen.query_one("#awake-stop").scroll_visible(animate=False)
                        await pilot.pause()
                        button = screen.query_one("#awake-stop")
                        self.assertLessEqual(button.region.right, size[0])
                        self.assertLessEqual(button.region.bottom, size[1] - 1)
                    await self.click(pilot, screen, "#awake-stop")
                    self.assertIsInstance(app.screen, MaintenanceConfirm)
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertTrue(app.awake.running)
                    await app.action_quit()
                    self.assertIsInstance(app.screen, MaintenanceConfirm)
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertTrue(app.awake.running)
                    await self.click(pilot, screen, "#awake-stop")
                    await pilot.press("y")
                    await self.wait_for(pilot, lambda: not app.awake.running)
                    self.assertEqual(app.screen.query_one("#awake-command", Input).value, 'echo "a b"')

    async def test_unavailable_platform_cannot_start_a_session(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = SystemStewardApp(root=root, home=root, timeout=3)
            with patch("keep_awake_tui.available", return_value=False), patch("keep_awake.subprocess.Popen") as spawn:
                async with app.run_test(size=(80, 24)) as pilot:
                    await pilot.press("k")
                    await pilot.pause()
                    self.assertTrue(app.screen.query_one("#awake-start", Button).disabled)
                    self.assertIn("macOS only", str(app.screen.query_one("#awake-status").content))
                    spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
