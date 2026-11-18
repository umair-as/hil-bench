"""Shared paths for a service user, and the cross-user port check, offline."""

import errno
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from harness import TEST_CONFIG, FakePlug, HilTestCase, hil


class PathConfigTests(HilTestCase):
    def test_paths_from_config(self):
        shared = self.tmp / "srv"
        self.write_config(TEST_CONFIG + f'[paths]\nstate_dir = "{shared}/state"\nlog_dir = "{shared}/logs"\n'
                          f'lock_dir = "{shared}/locks"\n')
        self.use_fakes(None, FakePlug(state="OFF"))
        rc, out, err = self.run_main("lock")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["lock"], f"{shared}/locks/t.lock")
        rc, _, err = self.run_main("power", "on")
        self.assertEqual(rc, 0, err)
        self.assertTrue((shared / "state" / "actions.jsonl").exists())
        self.assertTrue((shared / "locks" / "t.lock").exists())
        self.assertEqual(hil.default_log_dir(), shared / "logs")

    def test_defaults_without_paths(self):
        self.write_config(TEST_CONFIG)
        rc, out, _ = self.run_main("lock")
        self.assertEqual(json.loads(out)["lock"], str(hil.STATE / "t.lock"))
        self.assertEqual(hil.default_log_dir(), hil.STATE / "logs")

    def test_lock_dir_defaults_to_state_dir(self):
        self.write_config(TEST_CONFIG + f'[paths]\nstate_dir = "{self.tmp}/s"\n')
        rc, out, _ = self.run_main("lock")
        self.assertEqual(json.loads(out)["lock"], f"{self.tmp}/s/t.lock")

    def test_relative_path_refused(self):
        self.write_config(TEST_CONFIG + '[paths]\nstate_dir = "rel/state"\n')
        rc, _, err = self.run_main("lock")
        self.assertEqual(rc, 2)
        self.assertIn("absolute", err)

    def test_unknown_paths_key_refused(self):
        self.write_config(TEST_CONFIG + '[paths]\nstat_dir = "/x"\n')
        rc, _, err = self.run_main("lock")
        self.assertEqual(rc, 2)
        self.assertIn("stat_dir", err)

    def test_creds_file_fallback_from_paths(self):
        creds = self.tmp / "plug-creds"
        creds.write_text("TASMOTA_USER=svc\nTASMOTA_PASS=pw\n")
        cfg = {"paths": {"creds_file": str(creds)}}
        hil.apply_paths(cfg)
        t = hil.Tasmota({"host": "192.0.2.1"})
        self.assertEqual(t.user, "svc")
        t2 = hil.Tasmota({"host": "192.0.2.1", "creds_file": str(creds)})
        self.assertEqual(t2.user, "svc")


class PortCheckTests(HilTestCase):
    def pty(self):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        return os.ttyname(slave)

    def test_free_pty_has_no_holders(self):
        path = self.pty()
        # Our own test process has the slave open; the check is about others.
        self.assertEqual([h for h in hil.port_busy(path) if f"[{os.getpid()}]" not in h], [])

    def test_flock_by_other_process_seen_via_proc_locks(self):
        path = self.pty()
        child = subprocess.Popen([sys.executable, "-c",
                                  "import fcntl, os, sys, time\n"
                                  "fd = os.open(sys.argv[1], os.O_RDWR | os.O_NOCTTY)\n"
                                  "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('locked', flush=True)\ntime.sleep(30)\n",
                                  path], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            # Pretend /proc/<pid>/fd of that process is unreadable (other user).
            with mock.patch.object(hil, "port_holders", lambda p: []):
                busy = hil.port_busy(path)
            self.assertTrue(any(str(child.pid) in b and "flock" in b for b in busy), busy)
        finally:
            child.kill()
            child.wait()

    def test_ebusy_open_reported_as_exclusive_holder(self):
        import serial
        self.write_config(TEST_CONFIG)
        bench = {"serial": {"port": self.pty()}}

        def busy(*a, **kw):
            raise serial.SerialException(errno.EBUSY, "could not open port: [Errno 16] Device or resource busy")
        with mock.patch("serial.Serial", busy), mock.patch.object(hil, "port_busy", lambda p: []):
            with self.assertRaisesRegex(hil.BenchError, "exclusive"):
                hil.open_serial(bench)


if __name__ == "__main__":
    unittest.main()
