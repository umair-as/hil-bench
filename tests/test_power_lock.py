"""Power control, the bench lock and the CLI entry point, offline."""

import json
import os
import socket
import unittest
import urllib.request

from harness import FakePlug, HilTestCase, hil, ns


class PowerTests(HilTestCase):
    def test_power_cycle_off_then_on(self):
        plug = FakePlug()
        calls = []
        t0 = hil.power_cycle(plug, 0.01, lambda: calls.append(plug.state))
        self.assertEqual(plug.calls, ["power Off", "power On"])
        self.assertEqual(calls, ["OFF"])
        self.assertIsInstance(t0, float)

    def test_power_cycle_turns_on_after_interrupt(self):
        plug = FakePlug()

        def boom():
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            hil.power_cycle(plug, 0.01, boom)
        self.assertEqual(plug.state, "ON")

    def test_cmd_power_on_takes_lock(self):
        plug = FakePlug(state="OFF")
        self.use_fakes(None, plug)
        with hil.bench_lock("t", "someone else"):
            pass  # lock released again
        rc = hil.cmd_power(ns(action="on"), "t", self.bench())
        self.assertEqual(rc, 0)
        self.assertEqual(plug.state, "ON")

    def test_creds_from_file(self):
        f = self.tmp / "creds"
        f.write_text("# plug\nexport TASMOTA_USER='admin'\nTASMOTA_PASS=\"s3cret\"\n")
        t = hil.Tasmota({"host": "192.0.2.1", "creds_file": str(f)})
        self.assertEqual((t.user, t.password), ("admin", "s3cret"))


class PowerCycleReplyLostTests(HilTestCase):
    def test_relay_switched_back_on_when_off_reply_is_lost(self):
        plug = FakePlug()
        plug.fail_after = {"Off"}
        with self.assertRaises(hil.BenchError):
            hil.power_cycle(plug, 0.01)
        self.assertEqual(plug.state, "ON")
        self.assertEqual(plug.calls, ["power Off", "power On"])


class ProxyTests(HilTestCase):
    """Plug credentials ride in the query string; a proxy must never see them."""

    def test_plug_requests_ignore_proxy_environment(self):
        import os
        os.environ["http_proxy"] = os.environ["HTTP_PROXY"] = "http://192.0.2.99:3128"
        seen = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *e):
                pass

            def read(self):
                return b'{"POWER1": "ON"}'

        def fake_open(opener, req, timeout=None):
            seen["handlers"] = [type(h).__name__ for h in opener.handlers]
            seen["proxies"] = [getattr(h, "proxies", None) for h in opener.handlers
                               if isinstance(h, urllib.request.ProxyHandler)]
            return Resp()

        t = hil.Tasmota({"host": "192.0.2.1"})
        from unittest import mock
        with mock.patch.object(urllib.request.OpenerDirector, "open", fake_open), \
                mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("global urlopen used")):
            self.assertEqual(t.power(), "ON")
        # No handler that would proxy: ProxyHandler({}) has no *_open methods,
        # so build_opener() doesn't even register it.
        self.assertFalse(any(seen["proxies"]), seen)
        # Control: the default opener would have used the proxy from the env.
        default = [h.proxies for h in urllib.request.build_opener().handlers
                   if isinstance(h, urllib.request.ProxyHandler)]
        self.assertIn("http", default[0])


class SmallFixTests(HilTestCase):
    def test_off_seconds_validated(self):
        plug = self.use_fakes(None, FakePlug())
        for bad in (-1.0, 0.0, float("nan"), float("inf")):
            with self.assertRaisesRegex(hil.BenchError, "--off-seconds"):
                hil.cmd_power(ns(action="cycle", off_seconds=bad), "t", self.bench())
        self.assertEqual(plug.calls, [])

    def test_off_seconds_explicit_value_used(self):
        self.use_fakes(None, FakePlug())
        rc = hil.cmd_power(ns(action="cycle", off_seconds=0.02), "t", self.bench())
        self.assertEqual(rc, 0)
        self.assertIn('"cycled_off_seconds": 0.02', hil.sys.stdout.getvalue())

    def test_benches_lists_bench_without_serial(self):
        self.write_config(MainTests.CONFIG + "[benches.noserial]\ndescription = \"power only\"\n")
        rc, out, err = self.run_main("benches")
        self.assertEqual(rc, 0, err)
        self.assertIn("noserial", out)
        self.assertIn("no serial", out)

    def test_fail_patterns_are_multiline_like_stages(self):
        _, fails, _ = hil.stage_config({"boot": {"fail_patterns": ["^resetting"]}}, 1)
        self.assertTrue(fails[0].search("U-Boot\nresetting ...\n"))


class LockTests(HilTestCase):
    def test_second_holder_is_refused_with_holder_info(self):
        with hil.bench_lock("t", "power cycle"):
            with self.assertRaises(hil.BenchLocked) as cm:
                with hil.bench_lock("t", "boot"):
                    pass
        self.assertIn("power cycle", str(cm.exception))
        self.assertIn(f"pid {os.getpid()}", str(cm.exception))

    def test_lock_is_per_bench(self):
        with hil.bench_lock("a", "x"), hil.bench_lock("b", "y"):
            pass


class GuardTests(HilTestCase):
    def test_network_is_blocked_in_tests(self):
        with self.assertRaises(AssertionError):
            socket.create_connection(("192.0.2.1", 80), timeout=0.1)

    def test_tasmota_cmd_never_reaches_network(self):
        t = hil.Tasmota({"host": "192.0.2.1"})
        with self.assertRaises((AssertionError, hil.BenchError)):
            t.cmd("Power1")


class MainTests(HilTestCase):
    CONFIG = """
default_bench = "t"
[benches.t]
description = "test bench"
[benches.t.power]
type = "tasmota"
host = "192.0.2.1"
[benches.t.serial]
port = "/dev/null-test"
"""

    def test_missing_config_exit_2(self):
        rc, _, err = self.run_main("benches")
        self.assertEqual(rc, 2)
        self.assertIn("no bench config", err)

    def test_benches_lists(self):
        self.write_config(self.CONFIG)
        rc, out, _ = self.run_main("benches")
        self.assertEqual(rc, 0)
        self.assertIn("* t", out)

    def test_unknown_bench_exit_2(self):
        self.write_config(self.CONFIG)
        rc, _, err = self.run_main("-b", "nope", "power", "status")
        self.assertEqual(rc, 2)
        self.assertIn("unknown bench", err)

    def test_locked_bench_exit_3(self):
        self.write_config(self.CONFIG)
        with hil.bench_lock("t", "boot"):
            rc, _, err = self.run_main("power", "off")
        self.assertEqual(rc, 3)
        self.assertIn("busy", err)


if __name__ == "__main__":
    unittest.main()
