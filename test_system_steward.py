"""Run with: python3 -m unittest -v"""
import steward_test_env  # noqa: F401 - isolates the Keep Awake state folder
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from system_steward import Entry, Scan, children, du_command, pretty_path, audit_entries


class ScannerTests(unittest.TestCase):
    def test_sizes_missing_paths_and_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "directory with spaces").mkdir()
            (root / "directory with spaces" / "data").write_bytes(b"x" * 65536)
            try:
                (root / "link").symlink_to(root / "directory with spaces", target_is_directory=True)
                has_symlink = True
            except (OSError, NotImplementedError):
                has_symlink = False
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
                scan = Scan([entry])
                self.assertTrue(scan.finished.wait(3))
                scan.close()
            self.assertEqual(entry.status, "partial")
            self.assertEqual(entry.size, 48 * 1024)

    def test_timeouts_and_cancellation_reap_processes(self):
        real_popen = subprocess.Popen
        for cancel in (False, True):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as folder:
                processes = []

                def slow_process(*args, **kwargs):
                    process = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
                    processes.append(process)
                    return process

                entry = Entry("slow", folder, "Projects")
                with patch("system_steward.subprocess.Popen", side_effect=slow_process):
                    scan = Scan([entry], timeout=30 if cancel else .05)
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
