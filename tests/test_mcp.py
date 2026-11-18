"""MCP server on a Unix socket: protocol, fixed tool list, agent policy."""

import json
import os
import socket
import stat
import threading
import unittest

from harness import TEST_CONFIG, FakePlug, FakeSerial, FakeUboot, HilTestCase, hil


class McpTests(HilTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(TEST_CONFIG + '[benches.t.uboot]\nab = "rauc"\nallow = ["printenv *"]\n')
        self.plug = FakePlug(state="OFF")
        self.use_fakes(None, self.plug)
        self.sock_path = self.tmp / "hil.sock"
        cfg = hil.load_config()
        hil.apply_paths(cfg)
        self.server = hil.McpServer(self.sock_path, cfg)
        self.server.bind()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.server.serve, args=(self.stop,), daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.conn.connect(str(self.sock_path))
        self.conn.settimeout(10)
        self.f = self.conn.makefile("rwb")
        self.next_id = 0

    def shutdown(self):
        self.f.close()
        self.conn.close()
        self.stop.set()
        self.thread.join(5)

    def send(self, msg):
        self.f.write((json.dumps(msg) + "\n").encode())
        self.f.flush()

    def rpc(self, method, params=None):
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        resp = json.loads(self.f.readline())
        self.assertEqual(resp["id"], self.next_id)
        return resp

    def call(self, name, **arguments):
        r = self.rpc("tools/call", {"name": name, "arguments": arguments})
        return r["result"]

    # ------------------------------------------------------------------

    def test_initialize_and_list(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "t", "version": "0"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", r["result"]["capabilities"])
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})  # no reply
        names = {t["name"] for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertEqual(names, {"benches", "lock_status", "power_status", "power", "boot", "uboot"})

    def test_no_raw_or_destructive_options_in_schemas(self):
        tools = self.rpc("tools/list")["result"]["tools"]
        names = {t["name"] for t in tools} | {k for t in tools for k in t["inputSchema"]["properties"]}
        for word in ("allow_destructive", "shell", "stop_string", "console", "hold", "log_dir", "attempts"):
            self.assertNotIn(word, names)
        uboot = next(t for t in tools if t["name"] == "uboot")
        self.assertEqual(uboot["inputSchema"]["properties"]["action"]["enum"],
                         ["env", "slot", "reset-counters", "run"])

    def test_power_on_recorded_as_agent_via_mcp(self):
        res = self.call("power", bench="t", action="on")
        self.assertFalse(res["isError"], res)
        self.assertEqual(self.plug.state, "ON")
        recs = [json.loads(l) for l in (hil.STATE / "actions.jsonl").read_text().splitlines()]
        self.assertEqual(recs[0]["caller"], "agent")
        self.assertEqual(recs[0]["via"], "mcp")

    def test_power_status_text(self):
        res = self.call("power_status")
        self.assertFalse(res["isError"])
        self.assertEqual(json.loads(res["content"][0]["text"])["relay"], "OFF")

    def test_unknown_argument_rejected(self):
        res = self.rpc("tools/call", {"name": "uboot", "arguments": {"action": "run", "commands": ["mmc list"],
                                                                     "allow_destructive": True}})
        self.assertIn("error", res)
        self.assertIn("allow_destructive", res["error"]["message"])

    def test_bad_types_and_enums_rejected(self):
        for args in ({"action": "shell"}, {"action": 1}, {"action": "run", "commands": "printenv"},
                     {"action": "run", "commands": [1]}, {"action": "env", "bench": "../x"},
                     {"action": "env", "then": "console"}):
            res = self.rpc("tools/call", {"name": "uboot", "arguments": args})
            self.assertIn("error", res, args)

    def test_unknown_tool(self):
        for name in ("hold", "console", "nope"):
            res = self.rpc("tools/call", {"name": name, "arguments": {}})
            self.assertIn("error", res)

    def test_uboot_run_obeys_agent_allowlist(self):
        res = self.call("uboot", action="run", commands=["mmc list"])
        self.assertTrue(res["isError"])
        self.assertIn("allow list", res["content"][0]["text"])

    def test_command_starting_with_dash_is_not_an_option(self):
        res = self.call("uboot", action="run", commands=["--allow-destructive"])
        self.assertTrue(res["isError"])
        self.assertIn("allow list", res["content"][0]["text"])

    def test_agent_policy_even_if_config_says_human(self):
        # HIL_CALLER=human can't loosen it either.
        os.environ["HIL_CALLER"] = "human"
        res = self.call("uboot", action="run", commands=["printenv x; saveenv"])
        self.assertTrue(res["isError"])
        self.assertIn("shell syntax", res["content"][0]["text"])

    def test_uboot_env_through_fake_board(self):
        ub = FakeUboot(env={"BOOT_ORDER": "A B", "BOOT_A_LEFT": "3", "BOOT_B_LEFT": "3"})
        ser = FakeSerial(responder=ub)
        from unittest import mock
        orig = self.plug.power

        def power(action=None):
            st = orig(action)
            if action == "On":
                ub.start(ser)
            return st
        self.plug.power = power
        with mock.patch.object(hil, "open_serial", lambda bench: ser):
            res = self.call("uboot", action="env", then="stay")
        self.assertFalse(res["isError"], res)
        self.assertEqual(json.loads(res["content"][0]["text"])["env_before"]["BOOT_ORDER"], "A B")

    def test_backspace_in_command_refused_via_mcp(self):
        res = self.call("uboot", action="run", commands=["printenv x\u0008\u0008saveenv"])
        self.assertTrue(res["isError"])
        self.assertIn("printable", res["content"][0]["text"])

    def test_stop_request_during_a_call_ends_the_server(self):
        from unittest import mock
        real = hil.dispatch

        def stopping_dispatch(*a, **kw):
            self.server.request_stop()  # what SIGTERM does mid-call
            return real(*a, **kw)
        with mock.patch.object(hil, "dispatch", stopping_dispatch):
            res = self.call("power_status")
        self.assertFalse(res["isError"])
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse(self.sock_path.exists())

    def test_parse_error_and_unknown_method(self):
        self.f.write(b"{not json\n")
        self.f.flush()
        self.assertEqual(json.loads(self.f.readline())["error"]["code"], -32700)
        self.assertEqual(self.rpc("resources/list")["error"]["code"], -32601)
        self.assertEqual(self.rpc("ping")["result"], {})

    def test_socket_is_private(self):
        mode = stat.S_IMODE(os.stat(self.sock_path).st_mode)
        self.assertEqual(mode, 0o600)


class McpBindTests(HilTestCase):
    def test_refuses_live_socket_and_replaces_stale(self):
        path = self.tmp / "s.sock"
        a = hil.McpServer(path, {})
        a.bind()
        with self.assertRaisesRegex(hil.BenchError, "already"):
            hil.McpServer(path, {}).bind()
        a.close()
        # Leave a stale socket file behind.
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(path))
        s.close()
        b = hil.McpServer(path, {})
        b.bind()
        b.close()

    def test_bind_error_is_bench_error(self):
        with self.assertRaisesRegex(hil.BenchError, "cannot listen"):
            hil.McpServer(self.tmp / ("x" * 120 + ".sock"), {}).bind()

    def test_refuses_non_socket_path(self):
        path = self.tmp / "file"
        path.write_text("x")
        with self.assertRaisesRegex(hil.BenchError, "not a socket"):
            hil.McpServer(path, {}).bind()


if __name__ == "__main__":
    unittest.main()
