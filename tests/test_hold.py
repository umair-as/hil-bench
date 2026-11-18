"""`hil hold`: run another tool under the bench lock, offline."""

import os
import sys
import unittest

from harness import TEST_CONFIG, FakePlug, HilTestCase, hil

PY = sys.executable


class HoldTests(HilTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(TEST_CONFIG)
        self.plug = FakePlug(state="ON")
        self.use_fakes(None, self.plug)

    def test_runs_command_and_returns_its_exit_code(self):
        rc, _, _ = self.run_main("hold", "--", PY, "-c", "import sys; sys.exit(7)")
        self.assertEqual(rc, 7)

    def test_needs_a_command(self):
        rc, _, err = self.run_main("hold")
        self.assertEqual(rc, 2)
        self.assertIn("command", err)

    def test_missing_program_is_bench_error(self):
        rc, _, err = self.run_main("hold", "--", "/nonexistent/flasher")
        self.assertEqual(rc, 2)

    def test_lock_held_while_command_runs(self):
        # The child reports whether a fresh (non-nested) lock attempt is refused.
        code = ("import fcntl, os, sys\n"
                "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
                "try:\n    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\nexcept BlockingIOError:\n    sys.exit(42)\n"
                "sys.exit(0)\n")
        rc, _, _ = self.run_main("hold", "--", PY, "-c", code, str(hil.STATE / "t.lock"))
        self.assertEqual(rc, 42)

    def test_other_callers_refused_while_held(self):
        # Simulate: hold is running; another hil call (not its child) tries power off.
        with hil.bench_lock("t", "hold: flash"):
            rc, _, err = self.run_main("power", "off")
        self.assertEqual(rc, 3)
        self.assertIn("hold: flash", err)
        self.assertEqual(self.plug.state, "ON")

    def test_holder_line_names_the_command(self):
        code = "import sys; print(open(sys.argv[1]).read())"
        out_file = self.tmp / "holder.txt"
        code = f"import sys; open({str(out_file)!r}, 'w').write(open(sys.argv[1]).read())"
        self.run_main("hold", "--", PY, "-c", code, str(hil.STATE / "t.lock"))
        self.assertIn("(hold: ", out_file.read_text())

    def test_child_env_names_bench_and_lock_fd(self):
        out_file = self.tmp / "env.txt"
        code = (f"import os; open({str(out_file)!r}, 'w').write("
                "os.environ['HIL_BENCH'] + ' ' + os.environ['HIL_LOCK_FD'])")
        self.run_main("hold", "--", PY, "-c", code)
        bench, fd = out_file.read_text().split()
        self.assertEqual(bench, "t")
        self.assertTrue(fd.isdigit())


class NestedLockTests(HilTestCase):
    """A command run under `hil hold` may call hil on the same bench: it
    inherits the lock's file descriptor and proves it holds the lock."""

    def test_inherited_fd_lets_nested_call_proceed(self):
        with hil.bench_lock("t", "hold: x") as fd:
            os.environ["HIL_LOCK_FD"] = str(fd)
            with hil.bench_lock("t", "power cycle"):
                pass
            # Nested exit must not release the outer lock or rewrite the holder.
            os.environ.pop("HIL_LOCK_FD")
            with self.assertRaises(hil.BenchLocked) as cm:
                with hil.bench_lock("t", "boot"):
                    pass
            self.assertIn("hold: x", str(cm.exception))

    def test_fd_of_own_open_is_not_proof(self):
        # Someone else holds the lock; a caller that opens the lock file itself
        # and claims that fd must still be refused.
        with hil.bench_lock("t", "hold: x"):
            own = os.open(hil.STATE / "t.lock", os.O_RDONLY)
            try:
                os.environ["HIL_LOCK_FD"] = str(own)
                with self.assertRaises(hil.BenchLocked):
                    with hil.bench_lock("t", "power off"):
                        pass
            finally:
                os.close(own)

    def test_fd_for_another_file_is_ignored(self):
        other = os.open(self.tmp / "other", os.O_RDWR | os.O_CREAT)
        try:
            os.environ["HIL_LOCK_FD"] = str(other)
            with hil.bench_lock("t", "hold: y"):
                with self.assertRaises(hil.BenchLocked):
                    with hil.bench_lock("t", "power off"):
                        pass
        finally:
            os.close(other)

    def test_bad_fd_value_is_ignored(self):
        for v in ("", "abc", "99999"):
            os.environ["HIL_LOCK_FD"] = v
            with hil.bench_lock("t", "power on"):
                pass


if __name__ == "__main__":
    unittest.main()
