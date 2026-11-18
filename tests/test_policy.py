"""Caller policy (human/agent), U-Boot allowlist and denylist, offline."""

import os
import unittest

from harness import TEST_CONFIG, FakeUboot, HilTestCase, hil, ns
from test_uboot import UbootHelpers


class CallerPolicyTests(HilTestCase):
    def test_default_is_human(self):
        self.assertEqual(hil.caller_policy({}, {}), "human")

    def test_env_can_set_agent(self):
        os.environ["HIL_CALLER"] = "agent"
        self.assertEqual(hil.caller_policy({}, {}), "agent")

    def test_config_global_and_per_bench(self):
        self.assertEqual(hil.caller_policy({"caller": "agent"}, {}), "agent")
        self.assertEqual(hil.caller_policy({}, {"caller": "agent"}), "agent")

    def test_env_cannot_loosen_config(self):
        os.environ["HIL_CALLER"] = "human"
        self.assertEqual(hil.caller_policy({"caller": "agent"}, {}), "agent")
        self.assertEqual(hil.caller_policy({}, {"caller": "agent"}), "agent")

    def test_unknown_value_fails_closed(self):
        for v in ("Human", "root", "", "admin"):
            os.environ["HIL_CALLER"] = v
            self.assertEqual(hil.caller_policy({}, {}), "agent", v)
        os.environ.pop("HIL_CALLER")
        self.assertEqual(hil.caller_policy({"caller": 1}, {}), "agent")

    def test_policy_recorded_in_action_log(self):
        import json
        self.write_config('caller = "agent"\n' + TEST_CONFIG)
        from harness import FakePlug
        self.use_fakes(None, FakePlug())
        self.run_main("power", "on")
        rec = json.loads((hil.STATE / "actions.jsonl").read_text().splitlines()[0])
        self.assertEqual(rec["caller"], "agent")


class DenylistTests(unittest.TestCase):
    BLOCKED = ["saveenv", "env save", "env erase", "env default -a", "setenv bootdelay -2",
               "setenv bootcmd run foo", "setenv bootcmd", "env set bootdelay 0", "env set -f bootcmd x",
               "env delete bootcmd", "editenv bootcmd", "fuse prog 0 1 0x1", "mw 0x80000000 0",
               "mw.l 0x1 0x2", "nm.b 0x10", "mm 0x10", "i2c mw 0x50 0 0", "i2c write 0 0x50 0 1",
               "run update", "printenv; mmc erase 0 1", "${cmd} write 0 0 0", "echo $bootcmd",
               "mmc erase 0 100", "env default -f -a", "ums 0 mmc 0", "sf erase 0 0x1000",
               "setexpr bootdelay 1 - 3", "env import -t 0x80000000"]
    ALLOWED = ["printenv", "printenv BOOT_ORDER", "setenv BOOT_ORDER A B", "mmc list", "version",
               "setenv bootcount 0", "bdinfo", "echo hello", "setenv runlevel 3"]

    def test_blocked(self):
        for c in self.BLOCKED:
            self.assertTrue(hil.is_destructive(c), c)

    def test_allowed(self):
        for c in self.ALLOWED:
            self.assertFalse(hil.is_destructive(c), c)


class AllowlistTests(unittest.TestCase):
    ALLOW = ["printenv", "printenv *", "setenv BOOT_ORDER *", "setenv BOOT_?_LEFT *", "mmc list"]

    def check(self, cmd):
        return hil.agent_command_refusal(cmd, self.ALLOW)

    def test_allowlisted_commands_pass(self):
        for c in ("printenv", "printenv BOOT_ORDER", "setenv BOOT_ORDER B A", "setenv BOOT_A_LEFT 3",
                  "mmc list", "  printenv   BOOT_ORDER "):
            self.assertIsNone(self.check(c), c)

    def test_not_allowlisted(self):
        for c in ("mmc dev 1", "setenv bootcmd x", "reset", "boot"):
            self.assertIn("not in", self.check(c), c)

    def test_shell_syntax_refused_before_matching(self):
        for c in ("setenv BOOT_ORDER A; mmc erase 0 1", "printenv && reset", "printenv || reset",
                  "printenv | x", "setenv BOOT_ORDER $x", "setenv BOOT_ORDER 'A B'", 'printenv "x"',
                  "printenv \\\\", "setenv BOOT_ORDER `x`", "printenv # c", "setenv BOOT_ORDER ${a}"):
            self.assertIn("shell syntax", self.check(c), c)

    def test_denylist_still_applies(self):
        self.assertIn("destructive", hil.agent_command_refusal("saveenv", ["saveenv"]))
        self.assertIn("destructive", hil.agent_command_refusal("run x", ["run *"]))

    def test_no_allowlist_refuses_everything(self):
        self.assertIn("allow", hil.agent_command_refusal("printenv", []))


class AgentUbootTests(UbootHelpers):
    def agent_bench(self, **uboot):
        return self.bench(uboot={"ab": "rauc", **uboot})

    def test_allow_destructive_refused(self):
        with self.assertRaisesRegex(hil.BenchError, "agent"):
            self.uboot(self.agent_bench(allow=["mmc *"]), action="run", commands=["mmc list"],
                       allow_destructive=True, caller="agent")

    def test_shell_refused(self):
        with self.assertRaisesRegex(hil.BenchError, "agent"):
            self.uboot(self.agent_bench(), action="shell", caller="agent")

    def test_stop_string_override_refused(self):
        with self.assertRaisesRegex(hil.BenchError, "stop-string"):
            self.uboot(self.agent_bench(), action="env", stop_string="xyz", caller="agent")

    def test_run_without_allowlist_refused(self):
        with self.assertRaisesRegex(hil.BenchError, "allow"):
            self.uboot(self.agent_bench(), action="run", commands=["printenv"], caller="agent")

    def test_run_allowlisted_command(self):
        ub = FakeUboot(env={"BOOT_ORDER": "A B"})
        self.setup_board(ub)
        rc, js = self.uboot(self.agent_bench(allow=["printenv *"]), action="run",
                            commands=["printenv BOOT_ORDER"], caller="agent")
        self.assertEqual(rc, 0, js)
        self.assertIn("printenv BOOT_ORDER", ub.executed)

    def test_slot_and_reset_counters_still_work_for_agent(self):
        for action, slot in (("slot", "B"), ("reset-counters", None)):
            ub = FakeUboot(env={"BOOT_ORDER": "A B", "BOOT_A_LEFT": "0", "BOOT_B_LEFT": "0"})
            self.setup_board(ub)
            rc, js = self.uboot(self.agent_bench(), action=action, slot=slot, caller="agent")
            self.assertEqual(rc, 0, js)
            self.assertIn("saveenv", ub.executed)

    def test_human_needs_flag_for_saveenv_via_run(self):
        with self.assertRaisesRegex(hil.BenchError, "destructive"):
            self.uboot(self.agent_bench(), action="run", commands=["saveenv"])

    def test_attempts_validated(self):
        for bad in (0, -1, 100):
            with self.assertRaisesRegex(hil.BenchError, "--attempts"):
                self.uboot(self.agent_bench(), action="reset-counters", attempts=bad)


class AgentConsoleTests(HilTestCase):
    def test_console_refused_for_agent(self):
        self.write_config(TEST_CONFIG)
        os.environ["HIL_CALLER"] = "agent"
        rc, _, err = self.run_main("console")
        self.assertEqual(rc, 2)
        self.assertIn("agent", err)

    def test_allow_destructive_refused_via_main(self):
        self.write_config('caller = "agent"\n' + TEST_CONFIG)
        rc, _, err = self.run_main("uboot", "run", "-c", "mmc list", "--allow-destructive")
        self.assertEqual(rc, 2)
        self.assertIn("--allow-destructive", err)


if __name__ == "__main__":
    unittest.main()
