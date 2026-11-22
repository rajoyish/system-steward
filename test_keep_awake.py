"""Keep Awake validation, subprocess ownership, and native UI lifecycles."""
import steward_test_env  # noqa: F401 - isolates the Keep Awake state folder
import asyncio
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from textual.widgets import Button, Checkbox, Input, Select

import awake_state
import keep_awake
from keep_awake import AwakeSession, plan_session
from keep_awake_tui import KeepAwakeScreen
from maintenance_tui import MaintenanceConfirm, MaintenanceScreen
from system_steward_tui import SystemStewardApp


REAL_POPEN = subprocess.Popen


def sleeping_task(argv, **kwargs):
    """Exercise real child cleanup without creating any macOS assertions."""
    return REAL_POPEN([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)


class DirectLauncher(keep_awake.Launcher):
    """Runs the reviewed argv as-is in the foreground (tests patch subprocess.Popen underneath)."""

    def command(self, plan):
        return plan.argv

    def detached(self, argv, cwd):
        return None


def use_direct_launcher(test):
    patcher = patch("keep_awake.default_launcher", DirectLauncher)
    patcher.start()
    test.addCleanup(patcher.stop)


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
        use_direct_launcher(self)
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
            with patch("keep_awake.check_pid", side_effect=ProcessLookupError), self.assertRaises(ValueError):
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

    def test_private_memory_reading(self):
        import windows_caffeinate
        mem = windows_caffeinate.get_process_memory_mb(os.getpid())
        self.assertIsNotNone(mem)
        self.assertGreater(mem, 0)
        self.assertIsNone(windows_caffeinate.get_process_memory_mb(0))

    def test_helper_stays_lean_and_uses_base_interpreter(self):
        probe = "import sys, windows_caffeinate; print(sorted({'subprocess', 'keep_awake', 'threading'} & set(sys.modules)))"
        result = subprocess.run([sys.executable, "-c", probe], cwd=Path(__file__).parent,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr)
        exe = Path(keep_awake.helper_python(windowless=True))
        self.assertTrue(exe.is_file())
        self.assertEqual(exe.name.lower(), "pythonw.exe")
        base = Path(getattr(sys, "_base_executable", sys.executable))
        if base.with_name("pythonw.exe").is_file():
            self.assertEqual(exe, base.with_name("pythonw.exe"))  # no venv launcher process in between

    def test_wait_for_pid_returns_on_exit_and_on_timeout(self):
        import windows_caffeinate
        quick = REAL_POPEN([sys.executable, "-c", "import time; time.sleep(.3)"])
        start = time.monotonic()
        windows_caffeinate.wait_for_pid(quick.pid, duration=10)
        self.assertLess(time.monotonic() - start, 5)
        quick.wait(timeout=5)
        slow = REAL_POPEN([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            start = time.monotonic()
            windows_caffeinate.wait_for_pid(slow.pid, duration=.4)
            self.assertLess(time.monotonic() - start, 3)
            self.assertIsNone(slow.poll())
        finally:
            slow.kill()
            slow.wait(timeout=5)

    def test_background_launch_status_and_stop_round_trip(self):
        helper = Path(__file__).parent / "windows_caffeinate.py"
        run = lambda *args: subprocess.run([sys.executable, str(helper), *args], capture_output=True,
                                           text=True, timeout=30, cwd=Path(__file__).parent)
        awake_state.remove()
        started = run("-b", "-id", "-t", "60")
        try:
            self.assertEqual(started.returncode, 0, started.stderr)
            record = awake_state.read_owned()
            self.assertIsNotNone(record, "launcher must write a verified record")
            self.assertTrue(record["display"])
            self.assertIn("private memory", run("--status").stdout)
            self.assertIn("already running", run("-b", "-i").stdout)
        finally:
            stopped = run("--stop")
        self.assertIn("stopped", stopped.stdout)
        deadline = time.monotonic() + 5
        while awake_state.process_alive(record["pid"]) and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertFalse(awake_state.process_alive(record["pid"]))
        self.assertIsNone(awake_state.read_owned())

    def test_terminating_helper_also_ends_wrapped_command(self):
        """The helper's kill-on-close job must not orphan the user's command (no killpg on Windows)."""
        helper = Path(__file__).parent / "windows_caffeinate.py"
        child = "import os, time; print(os.getpid(), flush=True); time.sleep(60)"
        proc = REAL_POPEN([sys.executable, str(helper), "-i", sys.executable, "-c", child],
                          stdout=subprocess.PIPE, text=True)
        try:
            child_pid = int(proc.stdout.readline())
            self.assertTrue(awake_state.process_alive(child_pid))
            proc.terminate()  # TerminateProcess, exactly what Stop session does
            proc.wait(timeout=5)
            deadline = time.monotonic() + 5
            while awake_state.process_alive(child_pid) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertFalse(awake_state.process_alive(child_pid), "wrapped command was orphaned")
        finally:
            proc.kill()
            proc.stdout.close()

    def test_unknown_helper_option_is_rejected(self):
        helper = Path(__file__).parent / "windows_caffeinate.py"
        result = subprocess.run([sys.executable, str(helper), "--bogus"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown option", result.stderr)

    def test_detached_launch_keeps_paths_out_of_powershell_source(self):
        hostile = [r"C:\it's\py.exe", "helper.py", "-i"]
        from platforms import windows as win
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout="4321\n", stderr="")
            self.assertEqual(win.spawn_detached(hostile, Path(r"C:\O'Brien")), 4321)
        argv, kwargs = run.call_args.args[0], run.call_args.kwargs
        self.assertNotIn("it's", " ".join(argv))
        self.assertNotIn("Brien", " ".join(argv))
        self.assertEqual(kwargs["env"]["SS_AWAKE_CMDLINE"], subprocess.list2cmdline(hostile))
        self.assertEqual(kwargs["env"]["SS_AWAKE_CWD"], r"C:\O'Brien")


class HelperArgumentTests(unittest.TestCase):
    def test_display_flag_only_from_flag_clusters(self):
        import windows_caffeinate as helper
        self.assertTrue(helper.parse_args(["-id"])["display"])
        self.assertTrue(helper.parse_args(["-i", "-d", "-t", "5"])["display"])
        options = helper.parse_args(["-i", "git", "--dry-run", "-d"])
        self.assertFalse(options["display"])  # flags of the wrapped command are not ours
        self.assertEqual(options["command"], ["git", "--dry-run", "-d"])
        self.assertEqual(helper.parse_args(["-i", "--", "-weird"])["command"], ["-weird"])
        self.assertEqual(helper.parse_args(["-w", "42", "-t", "1.5"])["pid"], 42)
        for bad in (["--bogus"], ["-t"], ["-t", "soon"], ["-w", "x"]):
            with self.subTest(args=bad), self.assertRaises(ValueError):
                helper.parse_args(bad)

    @unittest.skipUnless(sys.platform == "win32", "Windows helper dispatch")
    def test_default_launcher_dispatches_to_windows_helper(self):
        launcher = keep_awake.Launcher()
        background = launcher.command(plan_session(("i", "d"), duration="60", background=True))
        self.assertTrue(background[0].lower().endswith("pythonw.exe"))
        self.assertTrue(background[1].endswith("windows_caffeinate.py"))
        self.assertEqual(background[2:], ("--daemon", "-id", "-t", "60"))
        foreground = launcher.command(plan_session())
        self.assertTrue(foreground[0].lower().endswith("python.exe"))
        self.assertNotIn("--daemon", foreground)

    def test_plan_flags_and_record_display(self):
        self.assertEqual(plan_session().flags, "i")
        self.assertEqual(plan_session(("d", "i")).flags, "id")
        # A wrapped command or folder containing "d" must not switch on display sleep prevention.
        with tempfile.TemporaryDirectory(prefix="dddd ") as folder:
            plan = plan_session(mode="command", command="dd if=/dev/zero", cwd=folder)
            self.assertNotIn("d", plan.flags)


class OwnershipTests(unittest.TestCase):
    """The persisted PID is only trusted while it is still the exact process we launched."""

    def setUp(self):
        self.watched = REAL_POPEN([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(self.watched.wait, 5)
        self.addCleanup(self.watched.terminate)
        awake_state.remove()
        self.addCleanup(awake_state.remove)

    def test_background_session_persisted_restored_and_stopped(self):
        record = awake_state.record(self.watched.pid, mode="manual", flags="id", display=True, background=True)
        self.assertIsNotNone(record["create_time"])
        self.assertTrue(awake_state.save(record))
        self.assertEqual(awake_state.read_owned()["pid"], self.watched.pid)
        session = AwakeSession()
        self.assertTrue(session.running)
        self.assertTrue(session.background)
        self.assertEqual(session.plan.flags, "id")
        self.assertIn("background", session.status())
        session.stop()
        self.watched.wait(timeout=5)
        self.assertFalse(session.running)
        self.assertFalse(awake_state.state_file().exists())

    def test_reused_pid_is_never_adopted_or_terminated(self):
        record = awake_state.record(self.watched.pid, mode="manual", flags="i", background=True)
        record["create_time"] = "a different process"  # same PID, different process instance
        awake_state.save(record)
        self.assertFalse(awake_state.verify_owned(record))
        self.assertFalse(awake_state.terminate(record))
        session = AwakeSession()
        self.assertFalse(session.running)
        session.stop()
        self.assertIsNone(self.watched.poll(), "an unrelated process was terminated")
        self.assertFalse(awake_state.state_file().exists(), "stale record should be discarded")

    def test_foreign_executable_is_not_ours(self):
        identity = {"alive": True, "create_time": 1, "exe": r"C:\Windows\System32\notepad.exe"}
        with patch("awake_state.process_identity", return_value=identity):
            self.assertFalse(awake_state.verify_owned({"pid": 99, "create_time": 1}))
            self.assertFalse(awake_state.verify_owned({"pid": 99}))  # legacy record
        self.assertTrue(awake_state.owned_executable("/usr/bin/caffeinate"))
        self.assertTrue(awake_state.owned_executable(r"C:\venv\Scripts\pythonw.exe"))

    def test_running_is_pure_and_poll_ends_expired_timer(self):
        record = awake_state.record(self.watched.pid, mode="manual", flags="i", duration=5,
                                    started=time.time() - 60, background=True)
        awake_state.save(record)
        session = AwakeSession()
        with patch("awake_state.terminate", return_value=True) as terminate:
            self.assertTrue(session.running)
            self.assertEqual(session.remaining, 0)
            self.assertTrue(session.running)  # reading state never stops anything
            terminate.assert_not_called()
            self.assertFalse(session.poll())
            terminate.assert_called_once()
        self.assertIn("stopped", session.status())

    def test_record_file_is_written_atomically(self):
        awake_state.save({"pid": 1})
        leftovers = [p.name for p in awake_state.state_dir().iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class AwakeUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        use_direct_launcher(self)

    async def wait_for(self, pilot, predicate, timeout=15):
        # Wall-clock deadline: background workers run slower while the whole suite is busy.
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
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
                    from strings import TEXT
                    self.assertIn(TEXT.keep_awake_unavailable, str(app.screen.query_one("#awake-status").content))
                    spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
