"""Plug-side power cycle (Backlog) and PulseTime dead-man, offline.

The fake below models documented Tasmota behaviour (Backlog runs its
commands in order, Delay is in 0.1 s units, PulseTime switches the relay
off again after its time), plus what a real plug (Tasmota 14.3) showed:
any command it receives while a Backlog Delay runs changes that delay
(polling every 0.5 s cut 3 s to 0.6 s; polling every 0.05 s stalled it for
over 15 s). The fake records such commands in `disturbed`."""

import re
import threading
import time
import unittest
from unittest import mock

from harness import HilTestCase, hil, ns


class FakeTasmotaApi:
    def __init__(self, state="ON", step=0.0):
        self.state, self.pulsetime, self.log = state, 0, []
        self.step = step  # Tasmota's own pause between backlog commands
        self.lock = threading.Lock()
        self.timer = None
        self.in_delay = False
        self.disturbed: list[str] = []

    def _switch(self, on: bool):
        with self.lock:
            self.state = "ON" if on else "OFF"
        if self.timer:
            self.timer.cancel()
            self.timer = None
        if on and self.pulsetime:
            v = self.pulsetime
            secs = v / 10 if v <= 111 else v - 100
            self.timer = threading.Timer(secs, self._switch, (False,))
            self.timer.daemon = True
            self.timer.start()

    def run(self, command: str) -> dict:
        self.log.append(command)
        if self.in_delay and not command.startswith("Backlog"):
            self.disturbed.append(command)
        m = re.fullmatch(r"Power1(?: (On|Off))?", command)
        if m:
            if m.group(1):
                self._switch(m.group(1) == "On")
            return {"POWER1": self.state}
        m = re.fullmatch(r"PulseTime1 (\d+)", command)
        if m:
            self.pulsetime = int(m.group(1))
            return {"PulseTime1": {"Set": self.pulsetime}}
        if command.startswith("Backlog "):
            steps = [c.strip() for c in command[len("Backlog "):].split(";")]

            def go():
                for c in steps:
                    d = re.fullmatch(r"Delay (\d+)", c)
                    if d:
                        self.in_delay = True
                        time.sleep(int(d.group(1)) / 10)
                        self.in_delay = False
                    else:
                        self.run(c)
                    time.sleep(self.step)
            threading.Thread(target=go, daemon=True).start()
            return {"Backlog": "queued"}  # real reply content isn't relied on
        if command == "Status 8":
            return {"StatusSNS": {"ENERGY": {"Power": 3.0 if self.state == "ON" else 0.0}}}
        raise AssertionError(f"unexpected plug command {command!r}")


class PlugSideTests(HilTestCase):
    def plug(self, api, **conf):
        t = hil.Tasmota({"host": "192.0.2.1", **conf})
        p = mock.patch.object(t, "cmd", api.run)
        p.start()
        self.addCleanup(p.stop)
        return t

    # --- PulseTime encoding ---------------------------------------------

    def test_pulsetime_encoding(self):
        self.assertEqual(hil.pulsetime_value(0.5), 5)
        self.assertEqual(hil.pulsetime_value(11.1), 111)
        self.assertEqual(hil.pulsetime_value(12), 112)
        self.assertEqual(hil.pulsetime_value(600), 700)
        self.assertEqual(hil.pulsetime_value(64800), 64900)
        for bad in (0, -1, 0.05, 64801, float("nan")):
            with self.assertRaises(hil.BenchError):
                hil.pulsetime_value(bad)

    # --- plug-side cycle ------------------------------------------------

    def test_default_cycle_is_host_side(self):
        api = FakeTasmotaApi()
        t = self.plug(api)
        hil.power_cycle(t, 0.05)
        self.assertEqual(api.log, ["Power1 Off", "Power1 On"])

    def test_plug_side_cycle_uses_backlog_and_confirms_on(self):
        api = FakeTasmotaApi()
        t = self.plug(api, cycle_on_plug=True)
        seen_off = []
        hil.power_cycle(t, 0.5, lambda: seen_off.append(api.state), plug_side=True)
        self.assertEqual(api.state, "ON")
        self.assertIn("Backlog Power1 Off; Delay 5; Power1 On", api.log)
        self.assertEqual(seen_off, ["OFF"])
        # Host sent only the Backlog and one status query; Off/On came from the plug.
        self.assertEqual(api.log, ["Backlog Power1 Off; Delay 5; Power1 On", "Power1 Off", "Power1 On", "Power1"])

    def test_plug_side_cycle_sends_nothing_during_the_delay(self):
        api = FakeTasmotaApi()
        t = self.plug(api, cycle_on_plug=True)
        hil.power_cycle(t, 0.6, plug_side=True)
        self.assertEqual(api.disturbed, [])

    def test_boot_cycles_stay_host_side(self):
        # Boot and uboot need an exact t0 and a UART flush with the relay
        # open, which a plug-side cycle can't give; cycle_on_plug is ignored.
        api = FakeTasmotaApi()
        t = self.plug(api, cycle_on_plug=True)
        hil.power_cycle(t, 0.05)
        self.assertEqual(api.log, ["Power1 Off", "Power1 On"])

    def test_power_cycle_command_uses_plug_side(self):
        api = FakeTasmotaApi()
        t = self.plug(api, cycle_on_plug=True)
        self.use_fakes(None, t)
        hil.cmd_power(ns(action="cycle", max_on=None, off_seconds=0.3), "t", self.bench())
        self.assertTrue(any(c.startswith("Backlog") for c in api.log), api.log)
        self.assertEqual(api.disturbed, [])

    def test_plug_side_cycle_finishes_if_host_dies(self):
        api = FakeTasmotaApi()
        t = self.plug(api, cycle_on_plug=True)

        def die():
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            hil.power_cycle(t, 0.3, die, plug_side=True)
        time.sleep(0.6)
        self.assertEqual(api.state, "ON")

    def test_plug_side_cycle_off_seconds_range(self):
        t = self.plug(FakeTasmotaApi(), cycle_on_plug=True)
        for bad in (0.1, 361):
            with self.assertRaisesRegex(hil.BenchError, "0.2"):
                hil.power_cycle(t, bad, plug_side=True)

    def test_plug_side_cycle_relay_never_closes(self):
        api = FakeTasmotaApi()
        t = self.plug(api, cycle_on_plug=True)
        orig = api.run

        def stuck(c):
            if c.startswith("Backlog"):
                api._switch(False)
                return {}
            if c == "Power1 On":  # host fallback also fails to close it
                api.log.append(c)
                return {"POWER1": "OFF"}
            return orig(c)
        with mock.patch.object(t, "cmd", stuck), mock.patch.object(hil, "PLUG_CYCLE_GRACE", 0.3):
            with self.assertRaisesRegex(hil.BenchError, "relay"):
                hil.power_cycle(t, 0.2, plug_side=True)
        self.assertIn("Power1 On", api.log)  # host-side fallback was tried

    # --- dead-man ------------------------------------------------------------

    def test_max_on_arms_pulsetime_before_on(self):
        api = FakeTasmotaApi(state="OFF")
        t = self.plug(api, max_on_seconds=0.3)
        self.use_fakes(None, t)
        hil.cmd_power(ns(action="on", max_on=None), "t", self.bench())
        self.assertEqual(api.log[:2], ["PulseTime1 3", "Power1 On"])
        time.sleep(0.5)
        self.assertEqual(api.state, "OFF")  # the plug cut it on its own

    def test_flag_overrides_config_and_zero_disarms(self):
        api = FakeTasmotaApi(state="OFF")
        t = self.plug(api, max_on_seconds=600)
        self.use_fakes(None, t)
        hil.cmd_power(ns(action="on", max_on=0), "t", self.bench())
        self.assertEqual(api.log[:2], ["PulseTime1 0", "Power1 On"])

    def test_off_disarms_when_feature_configured(self):
        api = FakeTasmotaApi()
        t = self.plug(api, max_on_seconds=600)
        self.use_fakes(None, t)
        hil.cmd_power(ns(action="off", max_on=None), "t", self.bench())
        self.assertEqual(api.log, ["Power1 Off", "PulseTime1 0"])

    def test_pulsetime_untouched_by_default(self):
        api = FakeTasmotaApi(state="OFF")
        t = self.plug(api)
        self.use_fakes(None, t)
        hil.cmd_power(ns(action="on", max_on=None), "t", self.bench())
        hil.cmd_power(ns(action="off", max_on=None), "t", self.bench())
        hil.cmd_power(ns(action="cycle", max_on=None, off_seconds=0.05), "t", self.bench())
        self.assertFalse(any(c.startswith("PulseTime") for c in api.log), api.log)

    def test_cycle_arms_pulsetime(self):
        api = FakeTasmotaApi()
        t = self.plug(api, max_on_seconds=600)
        self.use_fakes(None, t)
        hil.cmd_power(ns(action="cycle", max_on=None, off_seconds=0.05), "t", self.bench())
        self.assertEqual(api.log, ["PulseTime1 700", "Power1 Off", "Power1 On"])

    def test_max_on_flag_only_for_on_and_cycle(self):
        self.use_fakes(None, self.plug(FakeTasmotaApi()))
        with self.assertRaisesRegex(hil.BenchError, "--max-on"):
            hil.cmd_power(ns(action="off", max_on=5), "t", self.bench())

    def test_bad_config_types(self):
        for conf in ({"max_on_seconds": "10"}, {"cycle_on_plug": "yes"}):
            with self.assertRaises(hil.BenchError):
                hil.Tasmota({"host": "192.0.2.1", **conf})


if __name__ == "__main__":
    unittest.main()
