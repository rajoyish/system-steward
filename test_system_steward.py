"""Run with: python3 -m unittest -v"""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import steward_test_env  # noqa: F401 - isolates the Keep Awake state folder
import system_steward
from system_steward import Entry, Scan, audit_entries, children, du_command, pretty_path


class ScannerTests(unittest.TestCase):
    def test_sizes_missing_paths_and_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "directory with spaces").mkdir()
            (root / "directory with spaces" / "data").write_bytes(b"x" * 65536)
            # A symlink (where the OS allows creating one) must be left out of the listing.
            with contextlib.suppress(OSError, NotImplementedError):
                (root / "link").symlink_to(root / "directory with spaces", target_is_directory=True)
            entries = children(root)
            self.assertEqual([e.name for e in entries], ["directory with spaces"])
            entries.append(Entry("missing", str(root / "absent"), "Projects"))
            scan = Scan(entries)
            try:
                self.assertTrue(scan.finished.wait(5))
                expected = int(subprocess.check_output(du_command(entries[0].path)).split()[0]) * 1024
                self.assertEqual(entries[0].size, expected)
                self.assertEqual(entries[0].status, "done")
                self.assertEqual(entries[1].status, "missing")
            finally:
                scan.close()

    def test_partial_result_is_not_reported_as_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            entry = Entry("restricted", folder, "Projects")
            with patch("system_steward.subprocess.Popen") as spawn:
                process = spawn.return_value
                process.communicate.return_value = ("48\t" + folder, None)
                process.returncode = 1
                scan = Scan([entry], in_process=False)
                self.assertTrue(scan.finished.wait(3))
                scan.close()
            self.assertEqual(entry.status, "partial")
            self.assertEqual(entry.size, 48 * 1024)

    def test_timeouts_and_cancellation_reap_processes(self):
        real_popen = subprocess.Popen
        for cancel in (False, True):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as folder:
                processes = []

                def slow_process(*args, processes=processes, **kwargs):
                    process = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
                    processes.append(process)
                    return process

                entry = Entry("slow", folder, "Projects")
                with patch("system_steward.subprocess.Popen", side_effect=slow_process):
                    scan = Scan([entry], timeout=30 if cancel else .05, in_process=False)
                    if cancel:
                        deadline = time.monotonic() + 2
                        while not processes and time.monotonic() < deadline:
                            time.sleep(.01)
                        scan.close()
                    self.assertTrue(scan.finished.wait(3))
                    scan.close()
                self.assertEqual(entry.status, "cancelled" if cancel else "timed out")
                self.assertTrue(processes)
                self.assertTrue(all(p.poll() is not None for p in processes))

class InProcessScanTests(unittest.TestCase):
    """The Windows default: in-process walk, no child processes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_counts_whole_clusters_and_never_spawns(self):
        (self.root / "a").mkdir()
        (self.root / "a" / "one").write_bytes(b"x" * 10)
        (self.root / "a" / "two").write_bytes(b"x" * 5000)
        cluster = system_steward.windows_cluster_size(self.root) if sys.platform == "win32" else 4096
        with patch("system_steward.windows_cluster_size", return_value=cluster), \
                patch("system_steward.subprocess.Popen") as spawn:
            scan = Scan([Entry("a", str(self.root / "a"), "Projects")], in_process=True)
            self.assertTrue(scan.finished.wait(5))
            scan.close()
        spawn.assert_not_called()
        self.assertEqual(scan.entries[0].status, "done")
        self.assertEqual(scan.entries[0].size, cluster * (1 + -(-5000 // cluster)))

    def test_cloud_placeholders_and_compressed_files(self):
        def stat(size, attributes):
            return type("S", (), {"st_size": size, "st_file_attributes": attributes})()
        self.assertEqual(system_steward.allocated_bytes(stat(10 ** 9, system_steward.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS), "x", 4096), 0)
        self.assertEqual(system_steward.allocated_bytes(stat(10 ** 9, system_steward.FILE_ATTRIBUTE_OFFLINE), "x", 4096), 0)
        with patch("system_steward.windows_on_disk_size", return_value=5000) as on_disk:
            self.assertEqual(system_steward.allocated_bytes(stat(10 ** 6, system_steward.FILE_ATTRIBUTE_COMPRESSED), "x", 4096), 8192)
            on_disk.assert_called_once_with("x", 10 ** 6)
        self.assertEqual(system_steward.allocated_bytes(stat(0, 0), "x", 4096), 0)
        self.assertEqual(system_steward.allocated_bytes(stat(1, 0), "x", 4096), 4096)

    @unittest.skipUnless(sys.platform == "win32", "junctions are Windows-only")
    def test_junctions_are_not_followed_or_double_counted(self):
        import _winapi
        real = self.root / "real"
        real.mkdir()
        (real / "data").write_bytes(b"x" * 65536)
        _winapi.CreateJunction(str(real), str(self.root / "current"))  # like Scoop's apps/<app>/current
        size, status = system_steward.tree_usage(str(self.root))
        self.assertEqual(status, "done")
        self.assertEqual(size, system_steward.tree_usage(str(real))[0])

    def test_unreadable_folder_is_partial_and_cancel_timeout_stop_the_walk(self):
        for name in ("ok", "locked"):
            (self.root / name).mkdir()
            (self.root / name / "f").write_bytes(b"x")
        real_scandir = os.scandir
        def scandir(path):
            if str(path).endswith("locked"):
                raise PermissionError("denied")
            return real_scandir(path)
        with patch("system_steward.os.scandir", side_effect=scandir):
            self.assertEqual(system_steward.tree_usage(str(self.root))[1], "partial")
        for _ in range(system_steward.CHECK_EVERY * 2):
            (self.root / "ok" / f"d{_}").mkdir()
        import threading
        cancelled = threading.Event()
        cancelled.set()
        self.assertEqual(system_steward.tree_usage(str(self.root), cancel=cancelled)[1], "cancelled")
        self.assertEqual(system_steward.tree_usage(str(self.root), deadline=time.monotonic() - 1)[1], "timed out")

    def test_single_file_and_symlink_roots(self):
        target = self.root / "file.bin"
        target.write_bytes(b"x" * 100)
        size, status = system_steward.tree_usage(str(target))
        self.assertEqual(status, "done")
        self.assertGreaterEqual(size, 100)


class LauncherTests(unittest.TestCase):
    def test_app_identity_and_version_launcher(self):
        from app_identity import APP_NAME, AUTHOR
        self.assertEqual(APP_NAME, "System Steward")
        self.assertEqual(AUTHOR, "Rajesh Budhathoki")
        if sys.platform == "win32":
            launcher = Path(__file__).parent.resolve() / "system-steward.ps1"
            cmd = ["pwsh", "-NoProfile", "-File", str(launcher), "--version"]
        else:
            launcher = Path(__file__).parent.resolve() / "system-steward"
            cmd = ["zsh", str(launcher), "--version"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("System Steward", result.stdout)
        self.assertIn("Rajesh Budhathoki", result.stdout)

    @unittest.skipUnless(sys.platform == "win32", "PowerShell launcher")
    def test_awake_status_and_stop_flags_never_start_a_session(self):
        launcher = Path(__file__).parent.resolve() / "system-steward.ps1"
        for flag in ("-Status", "-Stop"):
            with self.subTest(flag=flag):
                result = subprocess.run(["pwsh", "-NoProfile", "-File", str(launcher), "-Awake", flag],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("No ", result.stdout)  # "No ... Keep Awake session ..." (state dir is isolated)
                self.assertNotIn("✓", result.stdout)  # a start prints "✓ Keep Awake active ..."

    def test_launcher_from_another_working_directory(self):
        if sys.platform == "win32":
            launcher = Path(__file__).parent.resolve() / "system-steward.ps1"
            cmd_prefix = ["pwsh", "-NoProfile", "-File", str(launcher)]
        else:
            launcher = Path(__file__).parent.resolve() / "system-steward"
            cmd_prefix = ["zsh", str(launcher)]
        with tempfile.TemporaryDirectory(prefix="storage test ") as folder:
            (Path(folder) / "example").write_bytes(b"example")
            result = subprocess.run([*cmd_prefix, "--json", folder],
                                    cwd=folder, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["scope"], str(Path(folder).resolve()))
            self.assertEqual(data["entries"][0]["name"], "example")
            self.assertEqual(data["entries"][0]["status"], "done")

    def test_pretty_path_and_audit_entries(self):
        home = Path.home()
        self.assertEqual(pretty_path(home), "~")
        self.assertEqual(pretty_path(home / "subfolder"), "~/subfolder")
        entries = audit_entries()
        self.assertTrue(len(entries) > 0)
        self.assertTrue(all(isinstance(e, Entry) for e in entries))




if __name__ == "__main__":
    unittest.main()
