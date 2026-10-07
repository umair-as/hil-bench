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
line, so they don't show up in `ps` or in logs.

## Commands

| Command | Does |
|---|---|
| `hil benches` | list configured benches (`*` = default) |
| `hil power status` | relay state + W / V / A / kWh today (no lock taken) |
| `hil power on\|off\|cycle [--off-seconds N]` | relay control |
| `hil boot [--no-cycle] [--timeout S] [--log-dir D] [--json]` | power-cycle, watch the UART, time each boot stage, PASS/FAIL |
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
Required ones decide PASS. Any `fail_patterns` hit (kernel panic, bad FIT
hash, …) fails the run immediately. Both lists can be overridden per bench in
the config, e.g. to require the FIT signature line
(`Verifying Hash Integrity ... sha256,rsa4096:<key>+ OK`).

Each run writes `<bench>-boot-<timestamp>.log` (raw console bytes) and a
matching `.json` summary to `--log-dir`, defaulting to
`~/.local/state/hil-bench/logs/`. Grep raw logs with `grep -a`, because
consoles emit control bytes.

## U-Boot recovery (`hil uboot`)

Power-cycles the board (`--no-cycle` to catch a reset you trigger yourself),
stops the autoboot countdown, and works at the U-Boot shell. This gets a board
back from most failed-OTA states without pulling the SD card, as long as U-Boot
itself still runs.

| Action | Does |
|---|---|
| `env` | print the RAUC state (`BOOT_ORDER`, `BOOT_A_LEFT`, `BOOT_B_LEFT`, `bootcount`, `rauc_slot`) |
| `slot A\|B` | `BOOT_ORDER="<slot> <other>"`, both counters to `--attempts` (3), `saveenv` |
| `reset-counters` | both counters to `--attempts`, `saveenv`, e.g. after an "all slots exhausted" halt |
| `run -c CMD [-c CMD …]` | arbitrary commands in order; stops at the first non-zero `$?` |
| `shell` | stop autoboot, then hand the prompt to `tio` |

`--then boot` (default) runs `boot` and watches the console until login, so a
repair is checked by a real boot in the same run. `--then stay` leaves the
board at the prompt, and `--then reset` resets it.

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
- Assumes the RAUC U-Boot convention: `BOOT_ORDER` plus `BOOT_<slot>_LEFT`.

## Sharing the bench

- **One user at a time.** `power on/off/cycle` and `boot` hold
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
