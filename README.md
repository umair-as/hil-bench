# hil-bench

Shared hardware-in-the-loop bench control: switch a target's power, capture
its boot console, and assert it reached a login prompt. It is project-agnostic:
any Yocto tree (or an agent working in one) calls `hil` and doesn't need its
own copy of the bench details.

Requirements: Python ≥ 3.11 (`tomllib`), `pyserial`, and `tio` for `hil console`.

## Setup

```bash
mkdir -p ~/.config/hil-bench
cp examples/benches.toml.example ~/.config/hil-bench/benches.toml   # fill in
ln -s "$PWD/hil" ~/.local/bin/hil                                   # optional
```

Plug credentials stay in their own file (`TASMOTA_USER=` / `TASMOTA_PASS=`),
referenced by `creds_file`. `TASMOTA_USER`/`TASMOTA_PASS` in the environment
override it. The credentials are passed through urllib, never on a command
line, so they don't show up in `ps` or in logs. Requests to the plug always
go direct and ignore `http_proxy`/`HTTP_PROXY`, because the plug's `/cm` API
carries the credentials in the URL query string.

## Commands

| Command | Does |
|---|---|
| `hil benches` | list configured benches (`*` = default) |
| `hil power status` | relay state + W / V / A / kWh today (no lock taken) |
| `hil power on\|off\|cycle [--off-seconds N] [--max-on S]` | relay control; `--max-on` arms the plug's own switch-off timer (see "Plug-side safety") |
| `hil power watch [--interval S] [--duration S] [--csv F]` | sample W / V / A until the duration ends or Ctrl-C, then print avg/min/max W and Wh (no lock taken) |
| `hil boot [--no-cycle] [--timeout S] [--log-dir D] [--json]` | power-cycle, watch the UART, time each boot stage, PASS/FAIL |
| `hil boot --repeat N [--settle S]` | N cycles in a row (board kept up S s between runs); per-stage min/median/max/stdev, pass count |
| `hil boot --power-interval S` | also sample the plug every S s during the boot: avg/peak W and Wh up to the last stage |
| `hil uboot env\|slot A\|B\|reset-counters\|run -c CMD…\|shell` | stop autoboot, inspect/repair the U-Boot env (see below) |
| `hil wait-ssh [--host H] [--timeout S]` | wait for the sshd banner |
| `hil console [--log-dir D]` | interactive `tio` with logging |
| `hil lock` | who holds the bench lock, as JSON (read-only, never takes the lock) |
| `hil mcp --socket PATH [--socket-mode 600\|660]` | serve a small MCP tool set on a Unix socket, every call with the agent policy (see below) |
| `hil hold -- CMD [ARGS…]` | run CMD (flasher, installer, test script) under the bench lock; exits with CMD's code |

Select a bench with `-b NAME`, `HIL_BENCH`, or `default_bench`.

Exit codes: `0` pass, `1` target failed (stage missing, fail pattern,
ssh down), `2` bench/config/infra error, `3` bench locked by another run.

### Typical gate after flashing or an OTA install

```bash
hil boot --log-dir ./tio-session-logs \
  && hil wait-ssh --host "$TARGET" \
  && ./path/to/project-target-checks.sh "$TARGET"   # the project's own checks
```

## Boot stages

`hil boot` matches regexes against the console **in order**, and records the
time of each match measured from the moment the relay closed. Optional stages
can be absent (e.g. a quiet kernel console) and are reported as "not seen".
Required ones decide PASS. Any `fail_patterns` hit fails the run immediately.

The built-in defaults only assume Linux: an optional `Booting Linux` stage, a
required `login:` prompt, and `Kernel panic` as the only fail pattern.
Anything that depends on the bootloader or the image goes in the bench's
`[boot]` config. That covers EEPROM or U-Boot banners, UEFI stages, FIT
signature lines such as `Verifying Hash Integrity ... sha256,rsa4096:<key>+ OK`,
and U-Boot failure messages. See `examples/benches.toml.example` for a U-Boot +
signed-FIT bench.

Each run writes `<bench>-boot-<timestamp>.log` (raw console bytes) and a
matching `.json` summary to `--log-dir`, defaulting to
`~/.local/state/hil-bench/logs/`. Grep raw logs with `grep -a`, because
consoles emit control bytes.

## Timing and power measurement

Each stage line shows the time since relay close and, in brackets, the gap
from the previous stage, so the slow step in a boot stands out.

`--repeat N` holds the bench lock for the whole series. Each run gets its own
`<bench>-boot-rNN-<timestamp>.log/.json`, and the series ends with a
`<bench>-boot-series-<timestamp>.json` holding per-stage statistics over the
runs that reached that stage. The exit code is `0` only if every run passed.
With `--json`, only the series summary is printed. If the bench itself fails
mid-series (plug unreachable, Ctrl-C), a partial series file with an `aborted`
field is still written and the exit code is `2` (`130` for Ctrl-C).

`--repeat` cuts power as soon as the last stage matches. On an A/B bench (e.g.
RAUC) the boot counter is usually only reset when userspace marks the slot
good, so back-to-back cuts can use up a slot's attempts and switch slots
mid-series. Give such benches `--settle S`, which keeps the board up S seconds
after each run, long enough for the mark-good to happen. Adding a stage that
matches the booted slot also makes a switch visible in the results.

Power numbers come from the plug's meter. It refreshes about once a second
and is only as accurate as its calibration (see `docs/nous-a8t.md`), so treat
watts and Wh as indicative and compare runs on the same plug. The first
second after the relay closes can still show the off-state reading.
`--power-interval` sampling ends when the stage watch ends (last stage, fail
pattern, or timeout). A dropped plug reading is reported in the run's `power`
result but doesn't fail the boot, and `power watch` skips it, prints it to
stderr and counts it in its summary.

## U-Boot recovery (`hil uboot`)

Power-cycles the board (`--no-cycle` to catch a reset you trigger yourself),
stops the autoboot countdown, and works at the U-Boot shell. This gets a board
back from most failed-OTA states without pulling the SD card, as long as U-Boot
itself still runs.

| Action | Does |
|---|---|
| `env` | print the configured variables (below), or the whole `printenv` if none are configured |
| `slot A\|B` | `ab = "rauc"` only: `BOOT_ORDER="<slot> <other>"`, both counters to `--attempts` (3), `saveenv` |
| `reset-counters` | `ab = "rauc"` only: both counters to `--attempts`, `saveenv`, e.g. after an "all slots exhausted" halt |
| `run -c CMD [-c CMD …]` | arbitrary commands in order; stops at the first non-zero `$?` (agents: allowlist only) |
| `shell` | stop autoboot, then hand the prompt to `tio` (refused for agents) |

`--then boot` (default) runs `boot` and watches the console until login, so a
repair is checked by a real boot in the same run. Stages the console already
showed, in order, before the autoboot interrupt (an EEPROM or U-Boot banner)
can't repeat, so they are listed as `before_interrupt` and not expected
again, required or not. `--then stay` leaves the board at the prompt, and
`--then reset` resets it.

- **Stop string:** read from the autoboot prompt itself (`type 'xyz' to
  stop`), or `--stop-string` / `[benches.X.uboot] stop_string`. Plain
  "Hit any key" autoboot gets a space.
- **Production lockdown:** if the kernel starts loading before any countdown
  appears (`BOOTDELAY=-2`), the run fails with "not interruptible" and doesn't
  retry. The only way into such an image is reflashing.
- **Typing safety:** each command is typed without Enter and its echo checked
  first. A garbled echo gets Ctrl-C and one retry, and is never executed, so a
  dropped byte can't turn into a different `setenv`/`saveenv`.
- **Destructive commands** are refused without `--allow-destructive`:
  card/flash writes and erases (`mmc write/erase`, `sf`, `nand`, `gpt write`,
  `fatwrite`, `ums`, `dfu`, …), env writes (`saveenv`, `env save/default/
  erase/import`, `editenv`), changes to `bootdelay`/`bootcmd` (a saved
  `bootdelay -2` locks `hil uboot` out for good), `fuse` (OTP), memory and
  bus pokes (`mw`, `nm`, `mm`, `i2c mw/write`), `run`, and anything with `$`.
  A text denylist can't see through hush variables, hence the last two.
  `slot` and `reset-counters` run their own fixed `setenv`/`saveenv`
  sequence and don't need the flag.
- If a command fails, the board is left at the U-Boot prompt and the error
  says so; `hil power cycle` or `hil boot` brings it back.
- **A/B scheme is opt-in.** `[benches.X.uboot] ab = "rauc"` enables `slot`
  and `reset-counters`. These use RAUC's U-Boot convention (`BOOT_ORDER` plus
  `BOOT_<slot>_LEFT`), and the run reads those variables before and after.
  `env_vars = [...]` adds more variables to read (e.g. `bootcount`,
  `rauc_slot`). A bench without `ab` refuses `slot` and `reset-counters`.
- `hil uboot` only applies to boards that boot through U-Boot. A board with a
  different bootloader (e.g. UEFI) only uses `power`, `boot`,
  `wait-ssh` and `console`.

## Caller policy (human or agent)

A caller is either `human` (default) or `agent`. The policy comes from the
config and the environment the operator sets, never from a command-line
flag: `HIL_CALLER`, a top-level `caller = "agent"` in `benches.toml`, or
`caller = "agent"` in one bench. If any of them says `agent`, the caller is
an agent, and an unknown value also counts as `agent`. So the environment
can tighten the config but never loosen it.

For an agent caller:

- `--allow-destructive`, `uboot shell`, `console` and `--stop-string` are
  refused. Stop strings still work: `hil` types them from the autoboot
  prompt or `[uboot] stop_string`.
- `uboot run -c CMD` runs only commands that match the bench's allowlist,
  `[benches.X.uboot] allow = ["printenv", "printenv *", "setenv BOOT_ORDER *"]`
  (shell-style globs over the whole command, spaces collapsed). Without an
  allowlist, `run` is refused. Commands with hush syntax (`; & | $` quotes
  `\` `` ` `` `#` `{}` `<>`) are refused before matching, and the denylist
  above still applies to anything the allowlist lets through.
- `env`, `slot` and `reset-counters` stay available.
- Every action log line carries the caller.

On its own this is a promise, not enforcement: an agent running as the same
user can edit the config or call the plug directly. Run `hil` as a separate
service user to make it binding; see
[`docs/service-user.md`](docs/service-user.md). A `[paths]` table in
`benches.toml` (`state_dir`, `lock_dir`, `log_dir`, default `creds_file`)
moves state, locks, logs and credentials to a shared location for that.
Under a service user, give agents the MCP socket, not the CLI: run as the
service user, `hold`, `power watch --csv` and `--log-dir` go beyond what
the caller policy covers.

## MCP server (`hil mcp`)

`hil mcp --socket PATH` serves the Model Context Protocol (JSON-RPC 2.0, one
message per line, as on stdio) on a Unix socket, so an agent can drive the
bench without a shell. The operator starts it, ideally as the service user.
It has a small, fixed tool set:

| Tool | Arguments | Same as |
|---|---|---|
| `benches` | | `hil benches` |
| `lock_status` | `bench` | `hil lock` |
| `power_status` | `bench` | `hil power status` |
| `power` | `bench`, `action`: `on`/`off`/`cycle` | `hil power …` |
| `boot` | `bench`, `timeout`, `no_cycle` | `hil boot --json` |
| `uboot` | `bench`, `action`: `env`/`slot`/`reset-counters`/`run`, `slot`, `commands`, `then`: `boot`/`stay`/`reset` | `hil uboot … --json` |

- Every call runs through the same code as the CLI, with the **agent**
  caller policy forced, whatever the config or environment says. The
  config is read once, at start. Calls take the bench lock, go into the
  action log with `"via": "mcp"`, and `uboot run` only accepts the bench's
  allowlist.
- There is no tool for `console`, `uboot shell`, `hold` or
  `--allow-destructive`, and unknown arguments are rejected.
- The socket is created `0600` (`--socket-mode 660` lets the socket's group
  connect). Who may connect is decided by those permissions; for a
  service-user setup see [`docs/service-user.md`](docs/service-user.md).
- **One client at a time.** The server serves one connection until that
  client disconnects. A second client's connection is queued and gets no
  answer, not even to `initialize`, until then. Calls run one after the
  other; a running `boot` blocks the next call.
- SIGTERM or Ctrl-C lets a running call finish its cleanup (the relay goes
  back on) and reply, then stops the server and removes the socket. A
  stale socket from a crash is replaced on the next start.

A client that only speaks stdio connects through `socat`, e.g. in an MCP
client config:

```json
{"mcpServers": {"hil": {"command": "socat", "args": ["STDIO", "UNIX-CONNECT:/run/hil-bench/mcp.sock"]}}}
```

## Sharing the bench

- **One user at a time.** `power on/off/cycle`, `boot` and `uboot` hold
  `~/.local/state/hil-bench/<bench>.lock`. A second caller gets exit `3` and
  the holder's pid and command. Other tools can honour the same lock; the
  protocol (path, `flock(2)`, holder line) is in
  [`docs/bench-lock.md`](docs/bench-lock.md).
- **One reader on the UART.** A tty has no fan-out, so two readers each get
  part of the bytes. `hil boot`, `hil uboot` and `hil console` refuse if
  anything already has the port open (and name the process). That includes
  another user's `tio` or pyserial session (found through `/proc/locks`) and
  a tty held in `TIOCEXCL` mode. Before running, close any MCP
  serial session (`serial_close`) and quit `tio`.
- **Other tools under the lock.** The lock only covers `hil`'s own
  commands. Wrap anything that must not lose power halfway, such as a
  flasher, an update install over SSH, or an OTA upload, in
  `hil hold -- CMD`. Every other `hil` call on that bench then gets exit `3`
  until CMD exits. CMD runs with `HIL_BENCH` set and inherits the lock as
  file descriptor `HIL_LOCK_FD`, so a `hil` call inside CMD (e.g. a power
  cycle between flashing steps) reuses the lock instead of deadlocking.
  `hil` accepts that fd only if a non-blocking `flock` on it succeeds, which
  an fd from a fresh `open()` of the lock file doesn't while someone else
  holds it. A process CMD leaves running in the background keeps the fd,
  and so keeps the bench locked, until it exits. Errors before CMD starts
  exit `2`/`3`; after that the exit code is CMD's own (`128+N` for a
  signal).
- If a cycle is interrupted (Ctrl-C), the relay is always switched back on,
  so the board isn't left without power.
- **Action log.** Every state-changing command (`power on/off/cycle`, `boot`,
  `uboot`, `console`, `hold`) appends a `start` and an `end` line to
  `~/.local/state/hil-bench/actions.jsonl`: time, pid, user, bench, the
  command line, and on `end` the exit code and any error. Refused runs
  (bench locked, plug unreachable) are logged too. If the start line can't
  be written, the command doesn't run. `console` and `uboot shell` replace
  themselves with `tio`, so they only get a `start` line. Read-only
  commands (`power status/watch`, `benches`, `wait-ssh`) aren't logged.

## Power-loss behaviour of the plug

Tasmota `PowerOnState` decides what the relay does after the plug loses mains.
`3` (restore the last state) can leave a bench dark if it was off during an
outage. For an unattended bench, `PowerOnState 1` (always on) is the usual
choice.

## Plug-side safety (opt-in)

Two Tasmota features make the plug itself finish what the host started.
Both are off by default. Set them per bench in `[benches.X.power]`:

- **`cycle_on_plug = true`:** `power cycle` sends
  `Backlog Power1 Off; Delay N; Power1 On` and lets the plug run the cycle,
  so it finishes even if the host dies halfway (checked live by killing
  `hil` with `kill -9` mid-cycle). `hil` sends nothing to the plug until
  the off time is over, then confirms the relay is on, and switches it on
  from the host only as a fallback. The off time must be 0.2–360 s
  (Tasmota `Delay` limits).
  **Any other request to the plug during the off time changes it.** On a
  real plug, a status query every 0.5 s cut a 3 s `Delay` to 0.6 s, and
  one every 0.05 s stalled it for over 15 s. Don't run `power watch` or
  `power status` on that plug while it cycles. For the same reason `boot`
  and `uboot` always cycle from the host: they need the exact moment the
  relay closes, for stage timing, and a UART flush while it's open.
- **`max_on_seconds = S`:** a dead-man for unattended runs. Before every
  switch-on (`power on`, `power cycle`, `boot`, `uboot`), `hil` sets the
  plug's `PulseTime` so the plug switches itself off S seconds later, with
  or without the host. `power off` clears it again. `power on --max-on S`
  sets it for one call (`--max-on 0` clears it), even on a bench without
  the setting. Pick S longer than the whole run: a `boot --repeat` series
  re-arms it on every cycle, but `--settle` time counts too.

`PulseTime` is stored on the plug and also applies when someone presses the
plug's button. Without either setting or flag, `hil` never touches it.
The plug-side cycle was checked on a Tasmota 14.3 plug. `PulseTime` follows
the documented encoding but hasn't been checked on a real plug yet.

## License

MIT, see `LICENSE`.
