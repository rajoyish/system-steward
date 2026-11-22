"""Adobe cleanup always runs against fake Library folders in these tests."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from adobe_cleanup import AdobeCleanupJob, AdobePlan, adobe_running, build_plan, validate_target


class AdobeCleanupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.library = self.home / "Library"

    def put(self, relative):
        path = self.library / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
        return path

    def job(self, plan, running=False):
        with patch("adobe_cleanup.adobe_running", return_value=running):
            job = AdobeCleanupJob(plan)
            self.assertTrue(job.done.wait(5))
            job.thread.join()
        return job

    def test_all_supported_caches_clear_but_parent_folders_remain(self):
        relatives = [
            "Application Support/Adobe/Common/Media Cache/database",
            "Application Support/Adobe/Common/Media Cache Files/clip.cfa",
            "Application Support/Adobe/Common/Peak Files/version/clip.pek",
            "Caches/Adobe/After Effects/26.0/Disk Cache/frames/data",
            "Caches/Adobe/After Effects/24.0/Adobe After Effects 24.0 Disk Cache - Test.noindex/frames/data",
            "Caches/Adobe/Photoshop/Temporary Cache/tempfile",
            "Caches/Adobe/Illustrator/Temp/file",
            "Caches/Adobe/Bridge/Cache/data",
            "Caches/Adobe/Creative Cloud/TemporaryItems/data",
            "Caches/Adobe/Lightroom/Video/clip/data",
            "Caches/Adobe InDesign/Version 21/cache",
            "Caches/Adobe Camera Raw 2/entry.dat",
            "Caches/Adobe Camera Raw/entry.dat",
            "Caches/com.adobe.Photoshop/cache/data",
            "Caches/com.adobe.acc.AdobeDesktopService/cache.db",
        ]
        files = [self.put(relative) for relative in relatives]
        plan = build_plan(self.home)
        parents = plan.cache_dirs
        self.assertEqual(len(parents), len(relatives))
        result = self.job(plan)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.removed), len(files))
        self.assertTrue(all(not path.exists() for path in files))
        self.assertTrue(all(parent.is_dir() for parent in parents))
        self.assertTrue(all(not list(parent.iterdir()) for parent in parents))
        self.assertFalse(build_plan(self.home).targets)

    def test_catalogs_settings_and_unapproved_adobe_data_are_protected(self):
        protected = [
            "Preferences/Adobe/Photoshop/settings",
            "Application Support/Adobe/Common/ExportSettings/preset",
            "Application Support/Adobe/Common/Motion Graphics Templates/title.mogrt",
            "Application Support/Adobe/Plugins/plugin",
            "Application Support/Adobe/Scripts/script",
            "Caches/Adobe/Lightroom/catalog.lrcat",
            "Caches/Adobe/Photoshop/Workspace/settings",
            "Caches/Adobe InDesign/backup/catalog.LRCAT",
            "Caches/Adobe InDesign/catalog.lrcat-wal",
            "Caches/Adobe InDesign/catalog.lrcat-data/metadata",
            "Caches/Adobe InDesign/Presets/preset",
            "Caches/com.adobe.Photoshop/Plugins/plugin",
        ]
        preserved = [self.put(relative) for relative in protected]
        cleared = self.put("Caches/Adobe InDesign/disposable/cache")
        plan = build_plan(self.home)
        self.assertTrue(plan.skipped)
        result = self.job(plan)
        self.assertFalse(result.errors)
        self.assertFalse(cleared.exists())
        self.assertTrue(all(path.read_text() == "fixture" for path in preserved))
        for path in (self.library, self.library / "Preferences", self.library / "Application Support/Adobe",
                     self.library / "Caches/Adobe", self.library / "Caches/Adobe InDesign"):
            with self.subTest(path=path), self.assertRaises(OSError):
                validate_target(plan, path)

    def test_redirected_roots_and_symlinks_never_reach_external_files(self):
        outside = self.put("Outside/keep")
        root = self.library / "Caches/Adobe Camera Raw 2"
        root.mkdir(parents=True)
        (root / "link").symlink_to(outside.parent, target_is_directory=True)
        plan = build_plan(self.home)
        self.assertIn(root / "link", plan.skipped)
        self.assertFalse(self.job(plan).removed)
        root.rename(root.with_name("old-cache"))
        root.symlink_to(outside.parent, target_is_directory=True)
        plan = build_plan(self.home)
        self.assertIn(root, plan.skipped)
        self.assertTrue(outside.exists())

    def test_catalog_added_after_preview_blocks_the_entire_candidate(self):
        cache = self.put("Caches/Adobe InDesign/version/data")
        plan = build_plan(self.home)
        catalog = self.put("Caches/Adobe InDesign/version/precious.lrcat")
        result = self.job(plan)
        self.assertFalse(result.removed)
        self.assertTrue(result.errors)
        self.assertTrue(cache.exists())
        self.assertTrue(catalog.exists())

    def test_running_adobe_and_permission_errors_are_reported(self):
        target = self.put("Caches/Adobe Camera Raw/entry.dat")
        plan = build_plan(self.home)
        result = self.job(plan, running=True)
        self.assertTrue(result.errors)
        self.assertTrue(target.exists())
        with patch("adobe_cleanup.Path.unlink", side_effect=PermissionError("denied")):
            result = self.job(plan)
        self.assertFalse(result.removed)
        self.assertIn("denied", result.errors[0])

    def test_process_detection_and_unknown_status_block_cleanup(self):
        for command in ("/Applications/Adobe Photoshop 2026/Adobe Photoshop 2026.app/Contents/MacOS/Adobe Photoshop 2026",
                        "/Applications/Utilities/Adobe Creative Cloud/ACC/Creative Cloud.app/Contents/MacOS/Creative Cloud",
                        "/Library/Application Support/Adobe/Adobe Desktop Common/ADS/Adobe Desktop Service"):
            with self.subTest(command=command), patch("adobe_cleanup.subprocess.run", return_value=Mock(returncode=0, stdout=command)):
                self.assertTrue(adobe_running())
        with patch("adobe_cleanup.subprocess.run", return_value=Mock(returncode=0, stdout="/bin/zsh\n/usr/bin/python3")):
            self.assertFalse(adobe_running())
        with patch("adobe_cleanup.subprocess.run", return_value=Mock(returncode=1)):
            with self.assertRaises(OSError):
                adobe_running()

    def test_unapproved_root_cannot_be_forged_into_a_plan(self):
        keep = self.put("Application Support/Adobe/Common/ExportSettings/preset")
        plan = AdobePlan(self.library, (keep.parent,), (keep,), ())
        result = self.job(plan)
        self.assertTrue(result.errors)
        self.assertTrue(keep.exists())


    def test_missing_adobe_install_is_noop(self):
        plan = build_plan(self.home)
        self.assertFalse(plan.targets)
        self.assertFalse(self.job(plan).errors)

    def test_windows_adobe_common_and_local_layout(self):
        media_cache = self.home / "AppData/Roaming/Adobe/Common/Media Cache Files/video.cfa"
        disk_cache = self.home / "AppData/Local/Adobe/After Effects/Disk Cache/frame.data"
        protected_catalog = self.home / "AppData/Roaming/Adobe/Common/Media Cache Files/catalog.lrcat"
        export_preset = self.home / "AppData/Roaming/Adobe/Common/ExportSettings/preset"
        for p in (media_cache, disk_cache, protected_catalog, export_preset):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("fixture")
        plan = build_plan(self.home)
        self.assertIn(media_cache, plan.targets)
        self.assertIn(disk_cache, plan.targets)
        self.assertNotIn(export_preset, plan.targets)
        self.assertIn(protected_catalog, plan.skipped)
        result = self.job(plan)
        self.assertFalse(result.errors)
        self.assertFalse(media_cache.exists())
        self.assertFalse(disk_cache.exists())
        self.assertTrue(export_preset.exists())
        self.assertTrue(protected_catalog.exists())


if __name__ == "__main__":
    unittest.main()
