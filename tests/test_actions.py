"""Append-only record of state-changing commands, offline."""

import json
import unittest

from harness import TEST_CONFIG, FakePlug, HilTestCase, hil


class ActionLogTests(HilTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(TEST_CONFIG)
        self.plug = FakePlug(state="OFF")
        self.use_fakes(None, self.plug)

    def records(self):
        p = hil.STATE / "actions.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def test_power_on_logged_start_and_end(self):
        rc, _, _ = self.run_main("power", "on")
        self.assertEqual(rc, 0)
        recs = self.records()
        self.assertEqual([r["event"] for r in recs], ["start", "end"])
        self.assertEqual(recs[0]["bench"], "t")
        self.assertEqual(recs[0]["argv"], ["power", "on"])
        self.assertEqual(recs[1]["rc"], 0)
        self.assertEqual(recs[0]["pid"], recs[1]["pid"])
        for k in ("ts", "user", "pid"):
            self.assertIn(k, recs[0])

    def test_read_only_commands_not_logged(self):
        self.run_main("power", "status")
        self.run_main("benches")
        self.assertEqual(self.records(), [])

    def test_locked_run_is_logged_with_rc_3(self):
        with hil.bench_lock("t", "boot"):
            rc, _, _ = self.run_main("power", "off")
        self.assertEqual(rc, 3)
        self.assertEqual(self.records()[-1]["rc"], 3)
        self.assertIn("busy", self.records()[-1]["error"])

    def test_bench_error_logged_with_rc_2(self):
        self.plug.unreachable = True
        rc, _, _ = self.run_main("power", "on")
        self.assertEqual(rc, 2)
        self.assertEqual(self.records()[-1]["rc"], 2)

    def test_unwritable_log_refuses_the_action(self):
        hil.STATE.mkdir(parents=True, exist_ok=True)
        (hil.STATE / "actions.jsonl").mkdir()  # can't be opened as a file
        rc, _, err = self.run_main("power", "on")
        self.assertEqual(rc, 2)
        self.assertIn("action log", err)
        self.assertEqual(self.plug.state, "OFF")

    def test_log_is_appended_not_rewritten(self):
        self.run_main("power", "on")
        self.run_main("power", "off")
        self.assertEqual(len(self.records()), 4)


if __name__ == "__main__":
    unittest.main()
