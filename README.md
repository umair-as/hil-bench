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
| `hil power on\|off\|cycle [--off-seconds N]` | relay control |
| `hil power watch [--interval S] [--duration S] [--csv F]` | sample W / V / A until the duration ends or Ctrl-C, then print avg/min/max W and Wh (no lock taken) |
| `hil boot [--no-cycle] [--timeout S] [--log-dir D] [--json]` | power-cycle, watch the UART, time each boot stage, PASS/FAIL |
| `hil boot --repeat N [--settle S]` | N cycles in a row (board kept up S s between runs); per-stage min/median/max/stdev, pass count |
| `hil boot --power-interval S` | also sample the plug every S s during the boot: avg/peak W and Wh up to the last stage |
| `hil uboot env\|slot A\|B\|reset-counters\|run -c CMD…\|shell` | stop autoboot, inspect/repair the U-Boot env (see below) |
| `hil wait-ssh [--host H] [--timeout S]` | wait for the sshd banner |
| `hil console [--log-dir D]` | interactive `tio` with logging |

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
| `run -c CMD [-c CMD …]` | arbitrary commands in order; stops at the first non-zero `$?` |
| `shell` | stop autoboot, then hand the prompt to `tio` |

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
- **Destructive commands** (`mmc write/erase`, `env default`, `fatwrite`,
  `ums`, `dfu`, …) are refused without `--allow-destructive`.
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

## Sharing the bench

- **One user at a time.** `power on/off/cycle`, `boot` and `uboot` hold
  `~/.local/state/hil-bench/<bench>.lock`. A second caller gets exit `3` and
  the holder's pid and command.
- **One reader on the UART.** A tty has no fan-out, so two readers each get
  part of the bytes. `hil boot` and `hil console` refuse if anything already
  has the port open (and name the process). Before running, close any MCP
  serial session (`serial_close`) and quit `tio`.
- If a cycle is interrupted (Ctrl-C), the relay is always switched back on,
  so the board isn't left without power.

## Power-loss behaviour of the plug

Tasmota `PowerOnState` decides what the relay does after the plug loses mains.
`3` (restore the last state) can leave a bench dark if it was off during an
outage. For an unattended bench, `PowerOnState 1` (always on) is the usual
choice.

## License

MIT, see `LICENSE`.
