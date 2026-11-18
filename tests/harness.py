"""Offline test harness for `hil`: no plug, no UART, no real config.

Loads the extensionless `hil` script as a module and provides fakes for the
serial port, the smart plug and a U-Boot console. Every test runs with a
throwaway config/state directory, and network or serial access that isn't
faked raises instead of reaching real hardware.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

# Scrub the environment before `hil` computes its paths at import time.
_SCRUB = re.compile(r"^(HIL_|TASMOTA_|XDG_STATE_HOME$|https?_proxy$|HTTPS?_PROXY$|no_proxy$|NO_PROXY$|all_proxy$|ALL_PROXY$)")
for _k in [k for k in os.environ if _SCRUB.match(k)]:
    del os.environ[_k]
_IMPORT_DIR = tempfile.mkdtemp(prefix="hil-test-import-")
os.environ["HIL_BENCH_CONFIG"] = os.path.join(_IMPORT_DIR, "no-such-benches.toml")
os.environ["XDG_STATE_HOME"] = os.path.join(_IMPORT_DIR, "state")


def load_hil():
    loader = importlib.machinery.SourceFileLoader("hil", str(ROOT / "hil"))
    spec = importlib.util.spec_from_loader("hil", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hil"] = mod  # dataclasses look their module up here
    loader.exec_module(mod)
    return mod


hil = load_hil()


# ---------------------------------------------------------------- guards

_real_connect = socket.socket.connect


def _guarded_connect(self, address):
    if self.family == socket.AF_UNIX:
        return _real_connect(self, address)
    self.close()
    raise AssertionError(f"test tried to open a network connection to {address!r}")


def _no_serial(*a, **kw):
    raise AssertionError("test tried to open a real serial port")


def _no_exec(*a, **kw):
    raise AssertionError(f"test tried to replace the process (exec {a[:1]})")


# ----------------------------------------------------------------- fakes

class FakeSerial:
    """Scripted UART. `feed()` queues device output; `responder(fake, data)`
    is called on every write and can feed a reply."""

    def __init__(self, output: bytes | str = b"", responder=None, chunk: int = 4096):
        self.pending = bytearray()
        self.written = bytearray()
        self.responder = responder
        self.chunk = chunk
        self.lock = threading.Lock()
        self.feed(output)

    def feed(self, data: bytes | str) -> None:
        with self.lock:
            self.pending += data.encode("latin-1") if isinstance(data, str) else data

    def read(self, n: int) -> bytes:
        with self.lock:
            n = min(n, self.chunk)
            out, self.pending[:] = bytes(self.pending[:n]), self.pending[n:]
        if not out:
            time.sleep(0.001)
        return out

    def write(self, data: bytes) -> int:
        self.written += data
        if self.responder:
            self.responder(self, data)
        return len(data)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        with self.lock:
            self.pending.clear()

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakePlug:
    """Tasmota-like plug. `fail_after` = set of actions whose reply is lost
    after the relay has switched (BenchError raised, state changed)."""

    def __init__(self, state: str = "ON", watts: float = 3.5):
        self.state = state
        self.watts = watts
        self.calls: list[str] = []
        self.fail_after: set[str] = set()
        self.unreachable = False

    def power(self, action: str | None = None) -> str:
        self.calls.append(f"power {action}" if action else "power")
        if self.unreachable:
            raise hil.BenchError("tasmota fake unreachable")
        if action:
            self.state = {"On": "ON", "Off": "OFF"}.get(action, self.state)
            if action in self.fail_after:
                raise hil.BenchError(f"tasmota fake: reply to {action} lost")
        return self.state

    def energy(self) -> dict:
        self.calls.append("energy")
        if self.unreachable:
            raise hil.BenchError("tasmota fake unreachable")
        w = self.watts if self.state == "ON" else 0.0
        return {"watts": w, "volts": 230.0, "amps": round(w / 230, 3), "kwh_today": 0.1}

    def status(self) -> dict:
        return {"relay": self.power()} | self.energy()


class FakeUboot:
    """Responder that behaves like a U-Boot shell on a FakeSerial.

    Prints `banner` + an autoboot prompt as soon as the console is created
    (call `start(fake)`), echoes typed bytes, and runs a few commands."""

    PROMPT = "=> "

    def __init__(self, env: dict | None = None, banner: str = "U-Boot 2024.01\n",
                 autoboot: str = "Hit any key to stop autoboot:  2 ",
                 boot_output: str = "Starting kernel ...\n[    0.000000] Booting Linux on physical CPU 0x0\n"
                                    "Poky (Yocto) 5.0 board ttyAMA0\n\nboard login: ",
                 rc: dict | None = None):
        self.env = dict(env or {})
        self.banner, self.autoboot, self.boot_output = banner, autoboot, boot_output
        self.rc = rc or {}
        self.line = ""
        self.stopped = False
        self.executed: list[str] = []
        self.last_rc = 0

    def start(self, fake: FakeSerial) -> None:
        fake.feed(self.banner + self.autoboot)

    def __call__(self, fake: FakeSerial, data: bytes) -> None:
        text = data.decode("latin-1")
        if not self.stopped:
            self.stopped = True
            fake.feed(f"0\n{self.PROMPT}")
            return
        for ch in text:
            if ch == "\x03":
                self.line = ""
                fake.feed(f"<INTERRUPT>\n{self.PROMPT}")
            elif ch == "\r":
                fake.feed("\n" + self.run(self.line))
                self.line = ""
            else:
                self.line += ch
                fake.feed(ch)

    def run(self, cmd: str) -> str:
        self.executed.append(cmd)
        words = cmd.split()
        out, rc = "", self.rc.get(cmd, 0)
        if cmd.startswith("echo HILRC=$?"):
            return f"HILRC={self.last_rc}\n{self.PROMPT}"
        if not words:
            pass
        elif words[0] == "printenv":
            if len(words) == 1:
                out = "".join(f"{k}={v}\n" for k, v in self.env.items())
            elif words[1] in self.env:
                out = f"{words[1]}={self.env[words[1]]}\n"
            else:
                out, rc = f"## Error: \"{words[1]}\" not defined\n", 1
        elif words[0] == "setenv":
            if len(words) == 2:
                self.env.pop(words[1], None)
            else:
                self.env[words[1]] = " ".join(words[2:])
        elif words[0] == "saveenv":
            out = "Saving Environment to MMC... Writing to MMC(0)... OK\n"
        elif words[0] == "boot":
            self.last_rc = 0
            return self.boot_output
        elif words[0] == "reset":
            return "resetting ...\n"
        self.last_rc = rc
        return out + self.PROMPT


TEST_CONFIG = """
default_bench = "t"
[benches.t]
description = "test bench"
[benches.t.power]
type = "tasmota"
host = "192.0.2.1"
off_seconds = 0.01
[benches.t.serial]
port = "/dev/null-test"
"""

# ------------------------------------------------------------- test base

class HilTestCase(unittest.TestCase):
    """Isolated config/state per test; real network/serial access raises."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hil-test-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        env = {k: v for k, v in os.environ.items() if not _SCRUB.match(k)}
        env |= {"HOME": str(self.tmp / "home"), "HIL_BENCH_CONFIG": str(self.tmp / "benches.toml"),
                "XDG_STATE_HOME": str(self.tmp / "state")}
        for p in (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(hil, "CONFIG", self.tmp / "benches.toml"),
            mock.patch.object(hil, "PATHS", hil.Paths(state=self.tmp / "state" / "hil-bench")),
            mock.patch.object(socket.socket, "connect", _guarded_connect),
            mock.patch("sys.stdout", io.StringIO()),
            mock.patch("os.execv", _no_exec),
            mock.patch("os.execvp", _no_exec),
        ):
            p.start()
            self.addCleanup(p.stop)
        try:
            import serial  # noqa: F401
        except ImportError:
            pass
        else:
            p = mock.patch("serial.Serial", _no_serial)
            p.start()
            self.addCleanup(p.stop)

    # helpers ----------------------------------------------------------

    def write_config(self, text: str) -> Path:
        hil.CONFIG.write_text(text)
        return hil.CONFIG

    def bench(self, **extra) -> dict:
        b = {"power": {"type": "tasmota", "host": "192.0.2.1", "off_seconds": 0.01},
             "serial": {"port": "/dev/null-test", "baud": 115200}}
        for k, v in extra.items():
            b[k] = v
        return b

    def use_fakes(self, ser: FakeSerial, plug: FakePlug | None = None) -> FakePlug:
        plug = plug or FakePlug()
        for p in (mock.patch.object(hil, "open_serial", lambda bench: ser),
                  mock.patch.object(hil, "make_power", lambda bench: plug)):
            p.start()
            self.addCleanup(p.stop)
        return plug

    def console(self, ser: FakeSerial) -> "hil.Console":
        return hil.Console(ser, io.BytesIO(), time.monotonic())

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", ["hil", *argv]), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = hil.main()
        return rc, out.getvalue(), err.getvalue()


def ns(**kw):
    """argparse-like namespace with the defaults cmd_* functions expect."""
    import argparse
    base = dict(no_cycle=False, repeat=1, settle=0.0, power_interval=None, timeout=None,
                log_dir=None, json=True, slot=None, commands=[], then="stay", attempts=3,
                stop_string=None, allow_destructive=False, prompt_timeout=5, cmd_timeout=3,
                off_seconds=None, interval=1.0, duration=None, csv=None, caller="human", max_on=None)
    return argparse.Namespace(**(base | kw))
