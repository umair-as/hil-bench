"""Plug replies, console encoding, power watch output, JSON writes."""

import json
import unittest
import urllib.request
from unittest import mock

from harness import TEST_CONFIG, FakePlug, FakeSerial, HilTestCase, hil, ns


class PlugReplyTests(HilTestCase):
    def reply(self, raw: bytes):
        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *e):
                pass

            def read(self):
                return raw
        return mock.patch.object(urllib.request.OpenerDirector, "open", lambda *a, **k: Resp())

    def test_non_object_json_reply_is_bench_error(self):
        t = hil.Tasmota({"host": "192.0.2.1"})
        for raw in (b"[1, 2]", b"42", b'"x"'):
            with self.reply(raw), self.assertRaisesRegex(hil.BenchError, "unexpected reply"):
                t.power()

    def test_non_json_reply_is_bench_error(self):
        t = hil.Tasmota({"host": "192.0.2.1"})
        with self.reply(b"<html>login</html>"), self.assertRaisesRegex(hil.BenchError, "non-JSON"):
            t.power()


class ConsoleEncodingTests(HilTestCase):
    def test_latin1_round_trip_and_refusal(self):
        ser = FakeSerial()
        con = self.console(ser)
        con.write("setenv x é")
        self.assertEqual(bytes(ser.written), "setenv x é".encode("latin-1"))
        with self.assertRaisesRegex(hil.BenchError, "latin-1"):
            con.write("setenv x €")


class PowerWatchTests(HilTestCase):
    def test_cli_watch_defaults_to_one_second_interval(self):
        self.write_config(TEST_CONFIG)
        self.use_fakes(None, FakePlug())
        with mock.patch.object(hil.time, "monotonic", return_value=0), \
                mock.patch.object(hil.time, "sleep", side_effect=KeyboardInterrupt) as sleep:
            rc, out, err = self.run_main("power", "watch")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out.splitlines()[-1])["samples"], 1)
        sleep.assert_called_once_with(1.0)

    def test_csv_and_summary(self):
        csv = self.tmp / "w.csv"
        rc = hil.power_watch(ns(interval=0.02, duration=0.07, csv=str(csv)), FakePlug(watts=4.0))
        self.assertEqual(rc, 0)
        lines = csv.read_text().splitlines()
        self.assertEqual(lines[0], "t_s,watts,volts,amps")
        self.assertGreaterEqual(len(lines), 3)
        summary = json.loads(hil.sys.stdout.getvalue().splitlines()[-1])
        self.assertEqual(summary["max_w"], 4.0)

    def test_ctrl_c_ends_sampling_and_closes_csv(self):
        csv = self.tmp / "w.csv"
        plug = FakePlug()
        calls = {"n": 0}
        real = plug.energy

        def energy():
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt
            return real()
        plug.energy = energy
        rc = hil.power_watch(ns(interval=0.01, duration=None, csv=str(csv)), plug)
        self.assertEqual(rc, 0)
        self.assertEqual(len(csv.read_text().splitlines()), 3)  # header + 2 samples, flushed and closed
        self.assertEqual(json.loads(hil.sys.stdout.getvalue().splitlines()[-1])["samples"], 2)

    def test_unwritable_csv_is_bench_error(self):
        with self.assertRaisesRegex(hil.BenchError, "--csv"):
            hil.power_watch(ns(interval=0.01, duration=0.01, csv=str(self.tmp / "no" / "dir" / "w.csv")),
                            FakePlug())


class WriteJsonTests(HilTestCase):
    def test_atomic_write_leaves_no_temp_file(self):
        path = self.tmp / "x.json"
        hil.write_json(path, {"a": 1})
        self.assertEqual(json.loads(path.read_text()), {"a": 1})
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["x.json"])


if __name__ == "__main__":
    unittest.main()
