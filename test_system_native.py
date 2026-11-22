"""Native metric parsing and privileged plan tests; never modify real PAM files."""
import asyncio
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import native_maintenance
import system_actions
from system_status import (
    cpu_usage, network_rates, parse_battery, parse_netstat, parse_netstat_windows,
    parse_vm_stats, resource_score, SystemSampler, windows_cpu_usage, windows_drives, WindowsHost
)


class MetricTests(unittest.TestCase):
    def test_cpu_uses_interval_and_counter_rollover(self):
        self.assertEqual(cpu_usage((10, 10, 70, 10), (20, 20, 140, 20)), 30)
        self.assertIsNone(cpu_usage((1, 1, 1, 1), (1, 1, 1, 1)))
        self.assertEqual(cpu_usage((0xffffffff, 0, 0, 0), (1, 0, 2, 0)), 50)

    def test_memory_page_size_and_compressed_memory(self):
        output = 'Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages active: 100.\nPages inactive: 50.\nPages wired down: 20.\nPages occupied by compressor: 10.\n'
        result = parse_vm_stats(output, 16384 * 200)
        self.assertEqual(result['used'], 16384 * 130)
        self.assertEqual(result['used_percent'], 65)
        self.assertEqual(result['cached'], 16384 * 50)
        with self.assertRaises(ValueError):
            parse_vm_stats('bad output', 100)

    def test_network_deduplicates_ip_rows_excludes_loopback_and_handles_resets(self):
        sample = 'Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll\nen0 1500 <Link#4> abc 10 0 1000 8 0 500 0\nen0 1500 192.168 192.168.1.2 10 0 1000 8 0 500 0\nlo0 16384 <Link#1> localhost 2 0 10 2 0 10 0\n'
        self.assertEqual(parse_netstat(sample), {'en0': (1000, 500)})
        result = network_rates({'en0': (0, 0)}, {'en0': (2097152, 1048576)}, 2)
        self.assertEqual(result[0]['rx_rate_mbs'], 1)
        self.assertEqual(result[0]['tx_rate_mbs'], .5)
        self.assertIsNone(network_rates(None, {'en0': (1, 1)}, 1)[0]['rx_rate_mbs'])
        self.assertIsNone(network_rates({'en0': (5, 5)}, {'en0': (1, 1)}, 1)[0]['tx_rate_mbs'])
        self.assertTrue(native_maintenance.status_rows({'network': network_rates(None, {'en0': (1, 1)}, 1)}))

    def test_battery_and_resource_estimate_missing_data(self):
        self.assertEqual(parse_battery(" -InternalBattery-0 78%; discharging; 3:15 remaining")[0]['percent'], 78)
        self.assertEqual(parse_battery("Now drawing from 'AC Power'"), [])
        self.assertIsNone(resource_score({'cpu': {}, 'memory': {}})[0])
        self.assertEqual(resource_score({'cpu': {'usage': 95}, 'memory': {'used_percent': 90}, 'disks': [{'mount': '/', 'used_percent': 95}]})[0], 25)

    def test_failed_sampling_is_explicit_and_does_not_invent_a_score(self):
        sampler = SystemSampler(platform="darwin")
        with patch('system_status.MacHost', side_effect=OSError('blocked')), patch('system_status.command', side_effect=OSError('blocked')):
            sample = sampler.sample()
        self.assertIsNone(sample['health_score'])
        self.assertIn('System APIs', sample['_warnings'])
        self.assertIn('Network', sample['unavailable'])


class WindowsMetricTests(unittest.TestCase):
    def test_windows_cpu_usage_and_netstat_parsing(self):
        # In Windows: (idle, kernel, user); total = kernel + user; busy = (kernel - idle) + user
        self.assertEqual(windows_cpu_usage((0, 100, 0), (100, 200, 0)), 0.0)  # all idle
        self.assertEqual(windows_cpu_usage((0, 100, 100), (0, 150, 150)), 100.0)  # 0 idle
        sample_output = "Interface Statistics\n\nBytes                    1048576       524288\n"
        self.assertEqual(parse_netstat_windows(sample_output), {"Ethernet/Wi-Fi": (1048576, 524288)})

    def test_top_processes_uses_cpu_share_between_samples(self):
        from platforms import windows as winproc
        before = {(10, 1): ("busy.exe", 0, 100), (20, 1): ("idle.exe", 0, 100), (30, 1): ("reused.exe", 5_000_000, 100)}
        after = {(10, 1): ("busy.exe", 2 * 10_000_000, 400), (20, 1): ("idle.exe", 0, 100),
                 (30, 2): ("new-owner.exe", 9 * 10_000_000, 100)}  # same PID, different process
        rows = winproc.top_processes(before, after, elapsed=1.0, total_ram=1000, cpu_count=4)
        self.assertEqual([r["name"] for r in rows], ["busy", "idle"])  # reused PID has no baseline
        self.assertEqual(rows[0]["cpu"], 50.0)  # 2 CPU-seconds in 1 s on 4 CPUs = 50 % of capacity
        self.assertEqual(rows[0]["memory"], 40.0)
        self.assertTrue(all(0 <= r["cpu"] <= 100 for r in rows))

    @unittest.skipUnless(sys.platform == "win32", "native Windows APIs")
    def test_native_process_and_network_readings(self):
        import ctypes, os
        from platforms import windows as winproc
        self.assertEqual(ctypes.sizeof(winproc.MIB_IF_ROW2), 1352)  # netioapi.h layout guard
        self.assertEqual(winproc.MIB_IF_TABLE2.Table.offset, 8)
        names = winproc.process_names()
        self.assertIn(os.path.basename(sys.executable).lower(), names)
        snapshot = winproc.usage_snapshot()
        self.assertTrue(any(pid == os.getpid() for pid, _ in snapshot))
        for name, (rx, tx) in winproc.interface_counters().items():
            self.assertIsInstance(name, str)
            self.assertGreaterEqual(rx + tx, 0)

    @unittest.skipUnless(sys.platform == "win32", "native Windows APIs")
    def test_sampler_spawns_no_powershell(self):
        sampler = SystemSampler()
        real_run = subprocess.run
        spawned = []
        with patch("system_status.subprocess.run", side_effect=lambda argv, **kw: (spawned.append(argv), real_run(argv, **kw))[1]):
            sampler.sample()
            data = sampler.sample()
        self.assertEqual(spawned, [], "dashboard sampling should not start child processes")
        self.assertTrue(data["top_processes"])
        self.assertLessEqual(max(p["cpu"] for p in data["top_processes"]), 100)

    def test_windows_live_host_and_drives(self):
        if sys.platform != "win32":
            return
        host = WindowsHost()
        ticks = host.cpu_ticks()
        self.assertEqual(len(ticks), 3)
        mem = host.memory_stats()
        self.assertGreater(mem["total"], 0)
        self.assertGreater(mem["used"], 0)
        self.assertGreaterEqual(mem["used_percent"], 0)
        self.assertLessEqual(mem["used_percent"], 100)
        uptime = host.uptime_seconds()
        self.assertGreaterEqual(uptime, 0)
        drives = windows_drives()
        self.assertTrue(any(d["mount"].startswith("C:") for d in drives))


class TouchIDPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        (self.directory / 'sudo').write_text('auth include sudo_local\nauth required pam_opendirectory.so\n')

    def test_preview_enable_disable_preserve_unrelated_lines_without_writes(self):
        local = self.directory / 'sudo_local'
        original = '# custom config\nauth optional pam_smartcard.so\n#auth sufficient pam_tid.so\n'
        local.write_text(original)
        plan = system_actions.plan_touchid(True, self.directory)
        self.assertEqual(local.read_text(), original)
        self.assertIn('auth optional pam_smartcard.so', plan.after.decode())
        self.assertEqual(plan.after.decode().splitlines()[0], 'auth       sufficient     pam_tid.so')
        local.write_bytes(plan.after)
        self.assertTrue(system_actions.touchid_status(self.directory)['enabled'])
        disabled = system_actions.plan_touchid(False, self.directory)
        self.assertIn('# Disabled by System Steward:', disabled.after.decode())
        self.assertIn('auth optional pam_smartcard.so', disabled.after.decode())
        local.write_bytes(disabled.after)
        self.assertFalse(system_actions.touchid_status(self.directory)['enabled'])

    def test_absent_file_legacy_redirected_and_unsupported_configs(self):
        self.assertIsNone(system_actions.plan_touchid(True, self.directory).before)
        self.assertEqual(system_actions.plan_touchid(False, self.directory).after, b'')
        (self.directory / 'sudo').write_text('auth sufficient pam_tid.so\nauth include sudo_local\n')
        self.assertFalse(system_actions.touchid_status(self.directory)['supported'])
        with self.assertRaises(ValueError):
            system_actions.plan_touchid(False, self.directory)
        (self.directory / 'sudo').write_text('auth required pam_opendirectory.so\n')
        with self.assertRaises(ValueError):
            system_actions.plan_touchid(True, self.directory)
        (self.directory / 'sudo_local').symlink_to(self.directory / 'sudo')
        with self.assertRaises(ValueError):
            system_actions.read_pam(self.directory)

    def test_generated_script_parses_and_uses_atomic_backup_and_snapshot_checks(self):
        plan = system_actions.plan_touchid(True, self.directory)
        script = system_actions.touchid_script(plan)
        sh_cmd = shutil.which('sh') or '/bin/sh'
        parsed = subprocess.run([sh_cmd, '-n'], input=script, text=True, capture_output=True)
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        self.assertIn(plan.sudo_digest, script)
        self.assertIn('/bin/mv -f', script)
        self.assertIn('! -L', script)
        self.assertIn('mac-steward-lock', script)
        (self.directory / 'sudo_local').write_text('# existing\n')
        script = system_actions.touchid_script(system_actions.plan_touchid(True, self.directory))
        self.assertIn('/bin/cp -p', script)
        self.assertIn('system-steward-backup-', script)

    def test_transaction_in_fake_pam_directory_preserves_backup_and_refuses_stale_plan(self):
        local = self.directory / 'sudo_local'
        original = b'# preserve this setting\nauth optional pam_smartcard.so\n'
        local.write_bytes(original)
        plan = system_actions.plan_touchid(True, self.directory)
        with patch('system_actions.PAM_DIRECTORY', self.directory):
            script = system_actions.touchid_script(plan)
        # Exercise the transaction entirely as the test user in a temporary
        # directory. Root ownership is the only production step omitted.
        script = script.replace('/usr/sbin/chown root:wheel "$pam_tmp"', '/usr/bin/true')
        sh_cmd = shutil.which('sh') or '/bin/sh'
        result = subprocess.run([sh_cmd], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(local.read_bytes(), plan.after)
        backups = list(self.directory.glob('sudo_local.system-steward-backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        stale = subprocess.run([sh_cmd], input=script, text=True, capture_output=True)
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn('changed since review', stale.stderr)
        self.assertEqual(local.read_bytes(), plan.after)
        self.assertFalse((self.directory / '.mac-steward-lock').exists())


class NativeActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_actions_are_rejected_and_admin_runner_is_native(self):
        with patch('system_actions.run_process', AsyncMock()) as runner:
            unknown = system_actions.Action('bad', 'bad', 'bad', (('/usr/bin/true',),))
            with self.assertRaises(ValueError):
                await system_actions.run_action(unknown)
            runner.assert_not_awaited()
            with patch('system_actions.Path.is_file', return_value=True):
                await system_actions.run_action(system_actions.ACTIONS[0], platform='darwin')
            argv = runner.await_args.args[0]
            self.assertEqual(argv[0], '/usr/bin/osascript')
            self.assertIn('/usr/bin/dscacheutil', argv[-1])
            self.assertIn('«class badm»:true', argv[-1])
            # Windows runs its own reviewed command for the same task, never the macOS script.
            await system_actions.run_action(system_actions.ACTIONS[0], platform='win32')
            self.assertEqual(runner.await_args.args[0], list(system_actions.WINDOWS_COMMANDS['dns']))

    async def test_touchid_refuses_changed_preview_and_verifies_after_native_write(self):
        sudo = b'auth include sudo_local\n'
        import hashlib
        plan = system_actions.TouchIDPlan(True, b'# before\n', b'auth sufficient pam_tid.so\n', hashlib.sha256(sudo).hexdigest(), 'enable')
        with patch('system_actions.read_pam', return_value=(sudo, b'changed')), patch('system_actions.run_process', AsyncMock()) as runner:
            with self.assertRaisesRegex(ValueError, 'changed since review'):
                await system_actions.apply_touchid(plan)
            runner.assert_not_awaited()
        with patch('system_actions.read_pam', return_value=(sudo, plan.before)), patch('system_actions.touchid_status', return_value={'enabled': True, 'description': 'enabled'}), patch('system_actions.run_process', AsyncMock()) as runner:
            await system_actions.apply_touchid(plan)
            self.assertEqual(runner.await_args.args[0][0], '/usr/bin/osascript')
        with patch('system_actions.read_pam', return_value=(sudo, plan.before)), patch('system_actions.touchid_status', return_value={'enabled': False}), patch('system_actions.run_process', AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, 'verification'):
                await system_actions.apply_touchid(plan)


if __name__ == '__main__':
    unittest.main()
