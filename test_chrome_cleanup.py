"""Cleanup tests use temporary fake Chrome profiles; never the real browser."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from chrome_cleanup import CleanupJob, build_plan, chrome_running, validate_target


class ChromeCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.data = self.home / "Library/Application Support/Google/Chrome"
        self.cache = self.home / "Library/Caches/Google/Chrome"

    def put(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture data")
        return path

    def run_job(self, plan, running=False):
        with patch("chrome_cleanup.chrome_running", return_value=running):
            job = CleanupJob(plan)
            self.assertTrue(job.done.wait(5))
            job.thread.join()
        return job

    def test_cleans_all_profiles_and_caches_preserving_personal_data(self):
        deleted, preserved = [], []
        for profile in ("Default", "Profile 17", "Guest Profile", "System Profile"):
            root = self.data / profile
            for relative in ("Cookies", "Cookies-wal", "Network/Cookies", "Network/Cookies-journal",
                             "IndexedDB/site.blob/video", "Local Storage/leveldb/data",
                             "Session Storage/data", "Service Worker/CacheStorage/data",
                             "WebStorage/42/IndexedDB/data", "WebStorage/QuotaManager",
                             "File System/data", "Code Cache/js/data", "GPUCache/data",
                             "Cache/Cache_Data/data", "DIPS", "QuotaManager-journal"):
                deleted.append(self.put(root / relative))
            for relative in ("Bookmarks", "History", "Login Data", "Login Data For Account",
                             "Preferences", "Extensions/extension/manifest.json",
                             "Local Extension Settings/extension/data", "Storage/ext/extension/data"):
                preserved.append(self.put(root / relative))
        deleted.append(self.put(self.cache / "Profile 17/Cache/data"))
        deleted.append(self.put(self.data / "ShaderCache/data"))
        preserved.extend([self.put(self.home / "Downloads/video.mp4"),
                          self.put(self.data / "Local State"),
                          self.put(self.home / "Library/Caches/Google/OtherApp/data")])
        plan = build_plan(self.home)
        self.assertEqual(len(plan.profiles), 4)
        job = self.run_job(plan)
        self.assertFalse(job.errors)
        self.assertTrue(all(not path.exists() for path in deleted))
        self.assertTrue(all(path.read_text() == "fixture data" for path in preserved))
        self.assertTrue((self.data / "Default").is_dir())
        self.assertFalse(build_plan(self.home).targets)

    def test_refuses_unsafe_targets_and_does_not_follow_symlinks(self):
        victim = self.put(self.home / "outside/keep")
        profile = self.data / "Default"
        profile.mkdir(parents=True)
        (profile / "IndexedDB").symlink_to(victim.parent, target_is_directory=True)
        self.put(profile / "Service Worker/cache/data")
        (profile / "Service Worker/external-link").symlink_to(victim.parent, target_is_directory=True)
        plan = build_plan(self.home)
        self.assertIn(profile / "IndexedDB", plan.skipped)
        for path in (self.data, profile, profile / "Bookmarks", victim, profile / "IndexedDB"):
            with self.subTest(path=path), self.assertRaises(OSError):
                validate_target(plan, path)
        self.assertFalse(self.run_job(plan).errors)
        self.assertEqual(victim.read_text(), "fixture data")

    def test_revalidates_paths_changed_after_preview(self):
        self.put(self.data / "Default/IndexedDB/data")
        victim = self.put(self.home / "outside/keep")
        plan = build_plan(self.home)
        path = self.data / "Default/IndexedDB"
        path.rename(path.with_name("old-indexeddb"))
        path.symlink_to(victim.parent, target_is_directory=True)
        job = self.run_job(plan)
        self.assertTrue(job.errors)
        self.assertFalse(job.removed)
        self.assertTrue(victim.exists())

    def test_blocks_running_chrome_and_failed_process_check(self):
        target = self.put(self.data / "Default/Cookies")
        job = self.run_job(build_plan(self.home), running=True)
        self.assertTrue(target.exists())
        self.assertFalse(job.removed)
        self.assertIn("Chrome is running", job.errors[0])
        with patch("chrome_cleanup.subprocess.run", return_value=Mock(returncode=2)):
            with self.assertRaises(OSError):
                chrome_running()

    def test_relaunch_stops_remaining_cleanup(self):
        self.put(self.data / "Default/Cookies")
        self.put(self.data / "Default/IndexedDB/data")
        plan = build_plan(self.home)
        with patch("chrome_cleanup.chrome_running", side_effect=[False, True]):
            job = CleanupJob(plan)
            self.assertTrue(job.done.wait(5))
            job.thread.join()
        self.assertEqual(len(job.removed), 1)
        self.assertTrue(plan.targets[1].exists())
        self.assertTrue(job.errors)

    def test_permission_errors_are_reported_without_claiming_success(self):
        self.put(self.data / "Default/IndexedDB/data")
        with patch("chrome_cleanup.shutil.rmtree", side_effect=PermissionError("denied")):
            job = self.run_job(build_plan(self.home))
        self.assertFalse(job.removed)
        self.assertIn("denied", job.errors[0])

    def test_no_chrome_data_is_a_noop(self):
        plan = build_plan(self.home)
        self.assertFalse(plan.targets)
        self.assertFalse(self.run_job(plan).errors)

    def test_windows_chrome_profile_layout(self):
        win_data = self.home / "AppData/Local/Google/Chrome/User Data"
        default_cookies = self.put(win_data / "Default/Network/Cookies")
        default_cache = self.put(win_data / "Default/Cache/Cache_Data/data")
        default_bookmarks = self.put(win_data / "Default/Bookmarks")
        shared_cache = self.put(win_data / "ShaderCache/data")
        plan = build_plan(self.home)
        self.assertIn("Default", plan.profiles)
        self.assertIn(default_cookies, plan.targets)
        self.assertIn(win_data / "Default/Cache", plan.targets)
        self.assertIn(win_data / "ShaderCache", plan.targets)
        self.assertNotIn(default_bookmarks, plan.targets)
        job = self.run_job(plan)
        self.assertFalse(job.errors)
        self.assertFalse(default_cookies.exists())
        self.assertFalse(default_cache.exists())
        self.assertFalse(shared_cache.exists())
        self.assertTrue(default_bookmarks.exists())


if __name__ == "__main__":
    unittest.main()
