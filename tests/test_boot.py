"""Boot-stage matching and the boot command, offline."""

import json
import unittest

from harness import FakePlug, FakeSerial, HilTestCase, hil, ns

LINUX_BOOT = ("U-Boot 2024.01\n"
              "Starting kernel ...\n"
              "[    0.000000] Booting Linux on physical CPU 0x0\n"
              "Poky 5.0 board ttyAMA0\n\n"
              "board login: ")


def result():
    return {"stages": {}, "missed": [], "fail_match": None}


class StageTests(HilTestCase):
    def watch(self, output, bench=None, timeout=0.5, chunk=4096):
        compiled, fails, _ = hil.stage_config(bench or {}, timeout)
        con = self.console(FakeSerial(output, chunk=chunk))
        r = result()
        ok = hil.watch_stages(con, compiled, fails, timeout, r)
        return ok, r

    def test_default_stages_pass(self):
        ok, r = self.watch(LINUX_BOOT)
        self.assertTrue(ok)
        self.assertEqual(list(r["stages"]), ["kernel", "login"])
        self.assertEqual(r["missed"], [])

    def test_optional_stage_may_be_silent(self):
        ok, r = self.watch("Poky 5.0 board ttyAMA0\n\nboard login: ")
        self.assertTrue(ok)
        self.assertEqual(r["missed"], ["kernel"])

    def test_missing_required_stage_fails(self):
        ok, r = self.watch("Booting Linux\n")
        self.assertFalse(ok)
        self.assertEqual(r["missed"], ["login"])

    def test_fail_pattern_stops_run(self):
        ok, r = self.watch("Booting Linux\nKernel panic - not syncing: VFS\n")
        self.assertFalse(ok)
        self.assertEqual(r["fail_match"], "Kernel panic")

    def test_last_login_line_is_not_a_login_prompt(self):
        ok, _ = self.watch("Booting Linux\nLast login: Tue\n")
        self.assertFalse(ok)

    def test_stages_must_appear_in_order(self):
        bench = {"boot": {"stages": [{"name": "a", "pattern": "AAA"}, {"name": "b", "pattern": "BBB"}]}}
        ok, r = self.watch("BBB\nAAA\n", bench)
        self.assertFalse(ok)
        self.assertEqual(list(r["stages"]), ["a"])

    def test_byte_at_a_time_output(self):
        ok, _ = self.watch(LINUX_BOOT, chunk=1, timeout=2)
        self.assertTrue(ok)


class BootCommandTests(HilTestCase):
    def test_boot_pass_writes_log_and_json(self):
        ser = FakeSerial()
        plug = FakePlug()
        self.use_fakes(ser, plug)
        # Output appears once the relay closes, like a real board.
        orig = plug.power

        def power(action=None):
            s = orig(action)
            if action == "On":
                ser.feed(LINUX_BOOT)
            return s
        plug.power = power
        args = ns(log_dir=str(self.tmp / "logs"), timeout=1)
        rc = hil.cmd_boot(args, "t", self.bench())
        self.assertEqual(rc, 0)
        self.assertEqual(plug.calls[:2], ["power Off", "power On"])
        js = list((self.tmp / "logs").glob("t-boot-*.json"))
        self.assertEqual(len(js), 1)
        data = json.loads(js[0].read_text())
        self.assertEqual(data["result"], "PASS")
        log = (self.tmp / "logs" / js[0].name.replace(".json", ".log")).read_bytes()
        self.assertIn(b"login: ", log)

    def test_boot_fail_exit_1(self):
        ser = FakeSerial()
        self.use_fakes(ser)
        args = ns(log_dir=str(self.tmp / "logs"), timeout=0.3)
        self.assertEqual(hil.cmd_boot(args, "t", self.bench()), 1)


class BufferTrimTests(HilTestCase):
    """The decoded buffer is capped; offsets kept across a trim must stay valid."""

    def test_stage_found_after_buffer_trim(self):
        noise = ("x" * 99 + "\n") * 9000          # ~900 KB before the kernel line
        tail = ("y" * 99 + "\n") * 2000           # ~200 KB after it
        out = noise + "Booting Linux on CPU 0\n" + tail + "board login: "
        compiled, fails, _ = hil.stage_config({}, 5)
        con = self.console(FakeSerial(out))
        r = result()
        ok = hil.watch_stages(con, compiled, fails, 5, r)
        self.assertTrue(ok, r)
        self.assertEqual(r["missed"], [])

    def test_fail_pattern_found_after_buffer_trim(self):
        out = ("x" * 99 + "\n") * 9000 + "Booting Linux\n" + ("y" * 99 + "\n") * 2000 + "Kernel panic\n"
        compiled, fails, _ = hil.stage_config({}, 5)
        r = result()
        self.assertFalse(hil.watch_stages(self.console(FakeSerial(out)), compiled, fails, 5, r))
        self.assertEqual(r["fail_match"], "Kernel panic")

    def test_expect_start_survives_trim(self):
        con = self.console(FakeSerial(("z" * 99 + "\n") * 10600 + "MARK\n"))
        start = con.mark()
        m = con.expect(hil.re.compile("MARK"), 5, start=start)
        self.assertIsNotNone(m)


class SeriesTests(unittest.TestCase):
    def test_series_stats(self):
        runs = [{"stages": {"kernel": 5.0, "login": 28.0}, "power": {"samples": 3, "wh": 0.02}},
                {"stages": {"kernel": 5.2, "login": 29.0}, "power": {"samples": 3, "wh": 0.03}},
                {"stages": {"login": 30.0}}]
        s = hil.series_stats(runs)
        self.assertEqual(s["kernel"]["n"], 2)
        self.assertEqual(s["login"]["median"], 29.0)
        self.assertEqual(s["energy_wh"]["n"], 2)

    def test_power_summary_trapezoid(self):
        s = hil.power_summary([{"t": 0, "watts": 2.0}, {"t": 1, "watts": 4.0}, {"t": 2, "watts": None}])
        self.assertEqual(s["samples"], 2)
        self.assertEqual(s["avg_w"], 3.0)
        self.assertEqual(s["wh"], round(3 / 3600, 4))


if __name__ == "__main__":
    unittest.main()
