"""Lock protocol helpers for other tools: /proc/locks lookup and `hil lock`."""

import fcntl
import json
import os
import subprocess
import sys
import time
import unittest

from harness import TEST_CONFIG, HilTestCase, hil


class ProcLocksTests(HilTestCase):
    def test_parse_proc_locks_line(self):
        st = os.stat(self.tmp)
        dev = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
        text = (f"1: FLOCK  ADVISORY  WRITE 4242 {dev} 0 EOF\n"
                f"1: -> FLOCK  ADVISORY  WRITE 4343 {dev} 0 EOF\n"
                f"2: POSIX  ADVISORY  WRITE 99 {dev} 0 EOF\n"
                "3: FLOCK  ADVISORY  WRITE 7 00:01:12345 0 EOF\n")
        self.assertEqual(hil.flock_holders(self.tmp, text), [4242])

    def test_real_flock_seen_in_other_process(self):
        path = self.tmp / "x.lock"
        path.write_text("")
        child = subprocess.Popen([sys.executable, "-c",
                                  "import fcntl, os, sys, time\n"
                                  "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
                                  "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('locked', flush=True)\ntime.sleep(30)\n",
                                  str(path)], stdout=subprocess.PIPE, text=True)
        with child:
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                self.assertEqual(hil.flock_holders(path), [child.pid])
            finally:
                child.kill()
        self.assertTrue(child.stdout.closed)
        self.assertEqual(hil.flock_holders(path), [])

    def test_missing_file_has_no_holders(self):
        self.assertEqual(hil.flock_holders(self.tmp / "nope"), [])


class LockCommandTests(HilTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(TEST_CONFIG)

    def test_free_bench(self):
        rc, out, _ = self.run_main("lock")
        self.assertEqual(rc, 0)
        info = json.loads(out)
        self.assertEqual(info["bench"], "t")
        self.assertFalse(info["held"])
        self.assertEqual(info["lock"], str(hil.PATHS.state / "t.lock"))

    def test_held_bench_reports_holder(self):
        with hil.bench_lock("t", "boot"):
            rc, out, _ = self.run_main("lock")
        info = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertTrue(info["held"])
        self.assertEqual(info["holder_pids"], [os.getpid()])
        self.assertIn("(boot)", info["holder"])

    def test_lock_does_not_take_the_lock(self):
        with hil.bench_lock("t", "boot"):
            self.run_main("lock")
            with self.assertRaises(hil.BenchLocked):
                with hil.bench_lock("t", "x"):
                    pass

    def test_stale_holder_line_is_not_held(self):
        with hil.bench_lock("t", "boot"):
            pass
        info = json.loads(self.run_main("lock")[1])
        self.assertFalse(info["held"])
        self.assertIn("(boot)", info["last_holder"])


if __name__ == "__main__":
    unittest.main()
