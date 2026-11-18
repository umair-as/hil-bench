"""hil reports its version, and records it in JSON summaries and the action log."""

import json
import re
import subprocess
import sys
import unittest
from unittest import mock

from harness import ROOT, TEST_CONFIG, FakePlug, FakeSerial, FakeUboot, HilTestCase, hil, ns

LOGIN = "Booting Linux\nboard login: "


class VersionStringTests(HilTestCase):
    def setUp(self):
        super().setUp()
        hil.version_string.cache_clear()
        self.addCleanup(hil.version_string.cache_clear)

    def test_semver_constant(self):
        self.assertRegex(hil.__version__, r"^\d+\.\d+\.\d+$")

    def test_git_checkout_adds_describe(self):
        with mock.patch.object(hil.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, "v1.0.0-3-gabc1234-dirty\n", "")):
            self.assertEqual(hil.version_string(), f"{hil.__version__} (v1.0.0-3-gabc1234-dirty)")

    def test_no_git_or_not_a_checkout_falls_back_to_release(self):
        for effect in (FileNotFoundError("git"), subprocess.TimeoutExpired("git", 2),
                       subprocess.CompletedProcess([], 128, "", "fatal: not a git repository")):
            hil.version_string.cache_clear()
            kw = {"side_effect": effect} if isinstance(effect, Exception) else {"return_value": effect}
            with mock.patch.object(hil.subprocess, "run", **kw):
                self.assertEqual(hil.version_string(), hil.__version__)

    def test_cli_version_flag(self):
        out = subprocess.run([sys.executable, str(ROOT / "hil"), "--version"], capture_output=True, text=True,
                             timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertRegex(out.stdout.strip(), rf"^hil {re.escape(hil.__version__)}( \(.+\))?$")


class RecordedVersionTests(HilTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(TEST_CONFIG)
        p = mock.patch.object(hil, "version_string", lambda: "9.9.9 (test)")
        p.start()
        self.addCleanup(p.stop)

    def board(self, output_on_power_on, responder=None):
        ser = FakeSerial(responder=responder)
        plug = self.use_fakes(ser, FakePlug())
        real = plug.power

        def power(action=None):
            st = real(action)
            if action == "On":
                output_on_power_on(ser)
            return st
        plug.power = power
        return ser

    def test_boot_and_series_json_and_action_log(self):
        self.board(lambda ser: ser.feed(LOGIN))
        logs = self.tmp / "logs"
        rc, _, err = self.run_main("boot", "--repeat", "2", "--timeout", "2", "--log-dir", str(logs))
        self.assertEqual(rc, 0, err)
        for js in logs.glob("*.json"):
            self.assertEqual(json.loads(js.read_text())["hil_version"], "9.9.9 (test)", js.name)
        self.assertEqual(len(list(logs.glob("*.json"))), 3)  # 2 runs + series
        recs = [json.loads(l) for l in hil.PATHS.actions.read_text().splitlines()]
        self.assertTrue(recs and all(r["hil_version"] == "9.9.9 (test)" for r in recs))

    def test_uboot_json(self):
        ub = FakeUboot()
        self.board(ub.start, responder=ub)
        logs = self.tmp / "logs"
        rc, _, err = self.run_main("uboot", "env", "--then", "stay", "--log-dir", str(logs))
        self.assertEqual(rc, 0, err)
        data = json.loads(next(logs.glob("t-uboot-*.json")).read_text())
        self.assertEqual(data["hil_version"], "9.9.9 (test)")

    def test_existing_json_key_order_unchanged(self):
        self.board(lambda ser: ser.feed(LOGIN))
        logs = self.tmp / "logs"
        self.run_main("boot", "--timeout", "2", "--log-dir", str(logs))
        keys = list(json.loads(next(logs.glob("t-boot-*.json")).read_text()))
        self.assertEqual(keys, ["bench", "started", "log", "cycled", "stages", "missed", "fail_match",
                                "elapsed", "result", "hil_version"])


if __name__ == "__main__":
    unittest.main()
