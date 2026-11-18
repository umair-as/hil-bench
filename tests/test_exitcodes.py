"""Bench, config and infrastructure failures exit 2, never 1 ("target failed")."""

import json
import unittest
from unittest import mock

from harness import TEST_CONFIG, FakePlug, FakeSerial, HilTestCase, hil

BOOT_OK = "Booting Linux\nboard login: "


class ExitCodeTests(HilTestCase):
    def setUp(self):
        super().setUp()
        self.plug = FakePlug()
        self.use_fakes(FakeSerial(), self.plug)

    def assert_rc2(self, *argv, says=None):
        rc, _, err = self.run_main(*argv)
        self.assertEqual(rc, 2, err)
        self.assertNotIn("Traceback", err)
        if says:
            self.assertIn(says, err)
        return err

    def test_toml_syntax_error(self):
        self.write_config("default_bench = \n")
        self.assert_rc2("benches", says="benches.toml")

    def test_bad_regex_in_fail_patterns(self):
        self.write_config(TEST_CONFIG + "[benches.t.boot]\nfail_patterns = ['(unclosed']\n")
        self.assert_rc2("boot", says="fail_patterns")

    def test_bad_regex_in_stages(self):
        self.write_config(TEST_CONFIG + "[[benches.t.boot.stages]]\nname = 'x'\npattern = '[bad'\n")
        self.assert_rc2("boot", says="stages")

    def test_stage_without_pattern(self):
        self.write_config(TEST_CONFIG + "[[benches.t.boot.stages]]\nname = 'x'\n")
        self.assert_rc2("boot", says="stages")

    def test_bad_regex_in_too_late_patterns(self):
        self.write_config(TEST_CONFIG + "[benches.t.uboot]\ntoo_late_patterns = ['(']\n")
        self.assert_rc2("uboot", "env", says="too_late_patterns")

    def test_non_positive_timeouts_rejected(self):
        self.write_config(TEST_CONFIG)
        for argv in (("boot", "--timeout", "0"), ("boot", "--timeout", "-5"),
                     ("uboot", "env", "--timeout", "0"), ("wait-ssh", "--host", "192.0.2.1", "--timeout", "0")):
            with self.subTest(argv=argv):
                self.assert_rc2(*argv, says="--timeout")

    def test_unwritable_lock_dir(self):
        blocker = self.tmp / "not-a-dir"
        blocker.write_text("x")
        self.write_config(TEST_CONFIG + f'[paths]\nlock_dir = "{blocker}/locks"\n')
        self.assert_rc2("power", "on", says="lock")

    def test_serial_drop_during_boot(self):
        ser = FakeSerial("U-Boot 2024.01\n")
        reads = {"n": 0}
        real_read = ser.read

        def read(n):
            reads["n"] += 1
            if reads["n"] > 3:
                raise OSError(5, "device reports readiness to read but returned no data")
            return real_read(n)
        ser.read = read
        self.use_fakes(ser, self.plug)
        self.write_config(TEST_CONFIG)
        log_dir = self.tmp / "logs"
        self.assert_rc2("boot", "--timeout", "5", "--log-dir", str(log_dir), says="serial")
        js = list(log_dir.glob("t-boot-*.json"))
        self.assertEqual(len(js), 1)
        self.assertEqual(json.loads(js[0].read_text())["result"], "ERROR")

    def test_unexpected_exception_is_exit_2_with_details(self):
        self.write_config(TEST_CONFIG)
        with mock.patch.object(hil, "cmd_power", side_effect=RuntimeError("surprise")):
            rc, _, err = self.run_main("power", "on")
        self.assertEqual(rc, 2)
        self.assertIn("internal error", err)
        self.assertIn("surprise", err)
        rec = json.loads((hil.STATE / "actions.jsonl").read_text().splitlines()[-1])
        self.assertEqual(rec["rc"], 2)


class ErrorHierarchyTests(unittest.TestCase):
    def test_exit_codes_live_on_the_exceptions(self):
        self.assertEqual(hil.BenchError("x").exit_code, 2)
        self.assertEqual(hil.BenchLocked("x").exit_code, 3)
        self.assertEqual(hil.UbootError("x").exit_code, 1)
        # "busy" must stay distinguishable from "broken"
        self.assertFalse(issubclass(hil.BenchLocked, hil.BenchError))


class ConfigValueTests(HilTestCase):
    """No fakes: values must be rejected before any plug request or port open
    (the harness turns real network/serial access into a test failure)."""

    def assert_rc2(self, *argv, says):
        rc, _, err = self.run_main(*argv)
        self.assertEqual(rc, 2, err)
        self.assertNotIn("Traceback", err)
        self.assertIn(says, err)

    def test_non_numeric_relay(self):
        self.write_config(TEST_CONFIG.replace('host = "192.0.2.1"', 'host = "192.0.2.1"\nrelay = "x"'))
        self.assert_rc2("power", "status", says="relay")

    def test_missing_host(self):
        self.write_config(TEST_CONFIG.replace('host = "192.0.2.1"', ""))
        self.assert_rc2("power", "status", says="host")

    def test_non_numeric_baud(self):
        self.write_config(TEST_CONFIG.replace('port = "/dev/null-test"', 'port = "/dev/null-test"\nbaud = "fast"'))
        self.assert_rc2("boot", says="baud")

    def test_bench_without_power_table(self):
        self.write_config('default_bench = "t"\n[benches.t]\n[benches.t.serial]\nport = "/dev/null-test"\n')
        for argv in (("power", "cycle"), ("power", "status")):
            self.assert_rc2(*argv, says="no [power] table")

    def test_non_numeric_off_seconds(self):
        self.write_config(TEST_CONFIG.replace("off_seconds = 0.01", 'off_seconds = "long"'))
        self.assert_rc2("power", "cycle", says="off_seconds")


if __name__ == "__main__":
    unittest.main()
