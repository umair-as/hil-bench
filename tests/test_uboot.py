"""U-Boot interrupt, typed commands and the uboot command, offline."""

import json
import unittest

from harness import FakePlug, FakeSerial, FakeUboot, HilTestCase, hil, ns


class UbootHelpers(HilTestCase):
    def setup_board(self, uboot: FakeUboot, plug: FakePlug | None = None):
        ser = FakeSerial(responder=uboot)
        plug = self.use_fakes(ser, plug)
        orig = plug.power

        def power(action=None):
            s = orig(action)
            if action == "On":
                uboot.start(ser)
            return s
        plug.power = power
        return ser, plug

    def uboot(self, bench, **kw):
        args = ns(log_dir=str(self.tmp / "logs"), **kw)
        rc = hil.cmd_uboot(args, "t", bench)
        js = sorted((self.tmp / "logs").glob("t-uboot-*.json"))
        return rc, (json.loads(js[-1].read_text()) if js else None)


class InterruptTests(HilTestCase):
    def test_interrupt_and_command(self):
        ub = FakeUboot(env={"bootcount": "1"})
        ser = FakeSerial(responder=ub)
        ub.start(ser)
        con = self.console(ser)
        seen = hil.uboot_interrupt(con, {}, None, 2)
        self.assertEqual(seen["stop_string"], " ")
        r = hil.uboot_rc(con, "printenv bootcount")
        self.assertEqual(r["output"], "bootcount=1")
        self.assertEqual(r["rc"], 0)

    def test_keyed_stop_string_detected(self):
        ub = FakeUboot(autoboot="Autoboot in 3 seconds, type 'xyz' to stop\n")
        ser = FakeSerial(responder=ub)
        ub.start(ser)
        seen = hil.uboot_interrupt(self.console(ser), {}, None, 2)
        self.assertEqual(seen["stop_string"], "xyz")

    def test_not_interruptible(self):
        ser = FakeSerial("U-Boot 2024.01\n## Loading kernel from FIT Image\n")
        with self.assertRaisesRegex(hil.UbootError, "not interruptible"):
            hil.uboot_interrupt(self.console(ser), {}, None, 1)

    def test_failed_command_rc(self):
        ub = FakeUboot()
        ser = FakeSerial(responder=ub)
        ub.start(ser)
        con = self.console(ser)
        hil.uboot_interrupt(con, {}, None, 2)
        self.assertEqual(hil.uboot_rc(con, "printenv nosuch")["rc"], 1)


class UbootCommandTests(UbootHelpers):
    def test_env_stay(self):
        ub = FakeUboot(env={"bootcount": "2", "rauc_slot": "A"})
        self.setup_board(ub)
        rc, js = self.uboot(self.bench(uboot={"env_vars": ["bootcount", "rauc_slot"]}), action="env")
        self.assertEqual(rc, 0)
        self.assertEqual(js["env_before"], {"bootcount": "2", "rauc_slot": "A"})

    def test_slot_on_rauc_bench(self):
        ub = FakeUboot(env={"BOOT_ORDER": "A B", "BOOT_A_LEFT": "0", "BOOT_B_LEFT": "3"})
        self.setup_board(ub)
        rc, js = self.uboot(self.bench(uboot={"ab": "rauc"}), action="slot", slot="B")
        self.assertEqual(rc, 0, js)
        self.assertEqual(js["env_after"], {"BOOT_ORDER": "B A", "BOOT_A_LEFT": "3", "BOOT_B_LEFT": "3"})
        self.assertIn("saveenv", ub.executed)

    def test_slot_needs_ab(self):
        with self.assertRaisesRegex(hil.BenchError, "A/B scheme"):
            self.uboot(self.bench(), action="slot", slot="A")

    def test_destructive_refused(self):
        with self.assertRaisesRegex(hil.BenchError, "destructive"):
            self.uboot(self.bench(), action="run", commands=["mmc erase 0 100"])

    def test_run_then_boot_pass(self):
        ub = FakeUboot()
        self.setup_board(ub)
        rc, js = self.uboot(self.bench(), action="run", commands=["printenv"], then="boot", timeout=2)
        self.assertEqual(rc, 0, js)
        self.assertEqual(js["result"], "PASS")
        self.assertIn("login", js["stages"])


class ThenBootStageTests(UbootHelpers):
    """--then boot judges only what follows `boot`; stages printed before the
    autoboot interrupt can't repeat."""

    STAGES = [{"name": "eeprom", "pattern": "BOOTLOADER", "optional": True},
              {"name": "uboot", "pattern": r"U-Boot \d{4}\.\d{2}"},
              {"name": "kernel", "pattern": "Booting Linux", "optional": True},
              {"name": "login", "pattern": r"^(?!Last )\S+ login: "}]

    def test_required_stage_before_interrupt_is_not_expected_again(self):
        ub = FakeUboot(banner="BOOTLOADER release\nU-Boot 2024.01\n")
        self.setup_board(ub)
        rc, js = self.uboot(self.bench(boot={"stages": self.STAGES}), action="env", then="boot", timeout=2)
        self.assertEqual(rc, 0, js)
        self.assertEqual(js["before_interrupt"], ["eeprom", "uboot"])
        self.assertEqual(js["missed"], [])

    def test_required_stage_not_seen_before_interrupt_stays_required(self):
        stages = self.STAGES[:2] + [{"name": "fit", "pattern": r"Verifying Hash Integrity.*OK"}] + self.STAGES[2:]
        ub = FakeUboot(banner="U-Boot 2024.01\n")
        self.setup_board(ub)
        rc, js = self.uboot(self.bench(boot={"stages": stages}), action="env", then="boot", timeout=1)
        self.assertEqual(rc, 1)
        self.assertEqual(js["before_interrupt"], ["uboot"])
        self.assertIn("fit", js["missed"])

    def test_stage_matching_out_of_order_is_not_dropped(self):
        # "login" text appears before the interrupt but after a required stage
        # that never printed, so it is not part of the in-order run seen.
        stages = [{"name": "spl", "pattern": "U-Boot SPL"}, {"name": "login", "pattern": "login: "}]
        ub = FakeUboot(banner="old login: \nU-Boot 2024.01\n")
        self.setup_board(ub)
        rc, js = self.uboot(self.bench(boot={"stages": stages}), action="env", then="boot", timeout=1)
        self.assertEqual(rc, 1)
        self.assertEqual(js["before_interrupt"], [])
        self.assertIn("spl", js["missed"])


class UbootBenchErrorTests(UbootHelpers):
    def test_bench_error_still_writes_json(self):
        plug = FakePlug()
        plug.unreachable = True
        self.setup_board(FakeUboot(), plug)
        with self.assertRaises(hil.BenchError):
            self.uboot(self.bench(), action="env")
        js = sorted((self.tmp / "logs").glob("t-uboot-*.json"))
        self.assertEqual(len(js), 1)
        data = json.loads(js[0].read_text())
        self.assertEqual(data["result"], "ERROR")
        self.assertIn("unreachable", data["error"])


if __name__ == "__main__":
    unittest.main()
