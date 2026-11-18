# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`hil` is a single-file Python CLI (no `.py` extension, no package) that
controls shared hardware-in-the-loop benches. It switches a board's power
through a Tasmota smart plug, captures its debug UART, checks the boot, and
measures boot timing and power draw. Board projects call it instead of keeping
their own copies of the bench details, and this repo must not depend on or
mention any of them.

Requirements: Python ≥ 3.11 (`tomllib`), `pyserial`, `tio` (for `hil console`).

## Keep the tool board-agnostic

Board-, bootloader- and image-specific values never go in `hil`. They live in
the host-local `~/.config/hil-bench/benches.toml` (override the path with
`HIL_BENCH_CONFIG`). `examples/benches.toml.example` is the committed
template. IPs, serial IDs and credential paths stay out of the repo.

- `DEFAULT_STAGES` / `DEFAULT_FAIL` only assume Linux: an optional
  `Booting Linux` stage, a required `login:` prompt, and `Kernel panic`.
  EEPROM/U-Boot/UEFI banners, FIT signature lines and U-Boot error strings go
  in a bench's `[boot] stages` / `fail_patterns`.
- `hil uboot` only applies to U-Boot boards. Its A/B commands (`slot`,
  `reset-counters`) are opt-in with `[uboot] ab = "rauc"` (RAUC's
  `BOOT_ORDER` + `BOOT_<slot>_LEFT`). Extra variables to read go in
  `[uboot] env_vars`. A bench whose board doesn't boot through U-Boot only
  uses `power`/`boot`/`wait-ssh`/`console`.
- Keep the repo self-contained: no paths to other projects, product or
  customer names, IPs, serial-adapter IDs or hostnames. Examples use
  placeholders (`192.0.2.x`, `usb-XXXX`).

## Architecture (all in `hil`)

- **Config → bench:** `load_config` + `get_bench` select a bench from
  `-b`, `HIL_BENCH`, or `default_bench`. Every `cmd_*` takes `(args, name, bench)`.
  `apply_paths` takes `STATE`, `LOCK_DIR`, `LOG_DIR` and a default
  `CREDS_FILE` from the config's `[paths]` (service-user setup,
  `docs/service-user.md`). Use `lock_path()` / `default_log_dir()`, not
  `STATE` directly.
- **Power:** `make_power` dispatches on `power.type`. Only `Tasmota` exists:
  it uses the HTTP `/cm` API with credentials from `creds_file` or
  `TASMOTA_USER`/`TASMOTA_PASS`. They go through urllib (never a proxy) and
  never into argv. `power_cycle` always switches the relay back on in a
  `finally`. Opt-in per bench: `cycle_on_plug` (`plug_side_cycle`, only for
  `power cycle`: Backlog Off/Delay/On, then **no plug traffic until the
  Delay is over**, because any command received meanwhile changes it on a
  real plug; then confirm ON, host On only as fallback. Boot/uboot always
  cycle host-side for an exact t0) and
  `max_on_seconds` (`arm_max_on` sets PulseTime before every switch-on;
  `power off` clears it).
- **Exclusivity:** `bench_lock` takes a per-bench `flock` on
  `lock_path(bench)` (`[paths] lock_dir`/`state_dir`, default
  `~/.local/state/hil-bench/<bench>.lock`) and writes the holder into it.
  The file stays after a run and isn't proof that the bench is busy; the
  flock is. `open_serial` refuses if any process has the tty open
  (`port_busy`: `port_holders` scans `/proc/<pid>/fd`, `flock_holders` reads
  `/proc/locks` for other users' flocks, and `EBUSY` means `TIOCEXCL`),
  because a tty has no fan-out. Close MCP
  serial sessions and `tio` first. `hil hold -- CMD` runs another tool under
  the lock and hands it down as `HIL_LOCK_FD`; `inherited_lock` accepts that
  fd only if a non-blocking `flock` on it succeeds, and nested use neither
  rewrites nor releases the lock.
- **Console:** logs raw bytes to the log file, keeps a decoded buffer capped
  at 1 MiB, and paces writes in 8-byte chunks (U-Boot UART has no flow control).
- **Boot checking:** `stage_config` compiles the bench's stages and fail
  patterns. `watch_stages` matches stages **in order** (optional ones may be
  skipped), scans only new text for fail patterns, and records the time of
  each stage from relay close.
- **U-Boot recovery:** `uboot_interrupt` detects the autoboot prompt (stop
  string auto-detected) and treats a kernel load seen before any prompt as a
  non-interruptible lockdown. `uboot_cmd` types each command, checks its echo
  before pressing Enter (Ctrl-C and one retry on a mismatch), and
  `uboot_rc` reads `$?`. `DESTRUCTIVE_RX` blocks erase/write/saveenv/
  bootdelay/bootcmd/fuse/mw/run/`$` commands without `--allow-destructive`. `--then boot` re-judges the boot
  after the repair in the same run.
- **Measurement:** `Tasmota.energy` reads the meter (`Status 8`).
  `PowerSampler` polls it in a thread during `boot --power-interval`, timed
  from relay close. Any exception in a reading is recorded as `error`, never
  raised, so a flaky plug can't fail a boot. `power_summary` does avg/min/max and trapezoid Wh. `boot --repeat`
  runs `boot_once` N times under one lock (with `--settle` between runs, for
  A/B benches whose boot counter needs a mark-good). `series_stats` reduces
  the runs to per-stage min/median/max/stdev, and a bench error mid-series
  still writes a partial series file. `power watch` is read-only and
  takes no lock.
- **Caller policy:** `caller_policy` returns `human` or `agent` from
  `HIL_CALLER` and the config's `caller` keys (any `agent` or unknown value
  wins; never a CLI flag). `main()` stores it in `args.caller`. For agents,
  `uboot_policy_check` refuses `--allow-destructive`, `shell` and
  `--stop-string`, and `agent_command_refusal` allows only `[uboot] allow`
  globs without hush syntax; `cmd_console` refuses too. Fixed actions
  (`env`, `slot`, `reset-counters`) and hil's own typed commands aren't
  filtered.
- **Action log:** `main()` wraps every state-changing command in
  `run_recorded`, which appends `start`/`end` JSON lines to
  `<state>/actions.jsonl` and refuses the command if the start line can't
  be written.
- **Outputs:** `boot` and `uboot` write `<bench>-<kind>-<ts>.log` (raw) plus a
  `.json` summary to `--log-dir` or `~/.local/state/hil-bench/logs/`.
  Grep the raw logs with `grep -a`.
- **Entry points:** `build_parser()` + `dispatch(args, argv, cfg, caller)`;
  `main()` is `dispatch(build_parser().parse_args(argv), argv)`.
- **MCP:** `hil mcp` (`McpServer`) maps each tool in `MCP_TOOLS` to a fixed
  argv (`mcp_argv`, values as `--opt=VALUE`) and runs it through
  `dispatch(..., cfg=<loaded at start>, caller="agent")` with stdout and
  stderr captured. Adding a tool means adding a schema, an argv mapping and
  a test. Never expose `console`, `hold`, `uboot shell` or
  `--allow-destructive`.
- **Exit codes:** `0` pass, `1` target failed a check (`UbootError` too),
  `2` bench/config/infra error (`BenchError`), `3` bench locked (`BenchLocked`).
  Config values go through `conf_number`/`compile_patterns`, serial I/O errors in
  `Console` become `BenchError`, and `dispatch` maps any other exception to `2`
  ("internal error" plus traceback), so `1` always means the target failed.

## Checking changes

There is no linter. CI (`.github/workflows/tests.yml`) runs the offline suite on Python 3.11 and 3.12 for every push to `main` and every pull request. The offline test suite needs no hardware: it loads `hil`
with `SourceFileLoader`, gives every test a throwaway config and state
directory, fakes the serial port, the plug and a U-Boot shell
(`tests/harness.py`), and makes any real network or serial access raise.
Add a failing test before every fix.

```bash
python3 -m unittest discover -s tests
python3 -m py_compile hil && ./hil --help >/dev/null
python3 -c "import tomllib; tomllib.load(open('examples/benches.toml.example','rb'))"
./hil benches                     # parses the real config
./hil -b <bench> power status     # read-only, takes no lock
```

To check the stage/fail logic against a real boot, feed a saved log from
`~/.local/state/hil-bench/logs/` to a `FakeSerial` from the harness and run
`stage_config` / `watch_stages` on it.

`power on/off/cycle`, `boot` and `uboot` act on real hardware that other
sessions or agents may be using. Check that nobody holds the bench lock or
the serial port before running them.

## Docs

- `README.md`: user-facing command reference. Keep its tables in sync with
  the argparse definitions in `main()`.
- `docs/bench-lock.md`: the lock protocol other tools follow. Keep it in
  sync with `bench_lock`, `inherited_lock` and `flock_holders`.
- `docs/nous-a8t.md`: generic notes on the bench smart plug (capabilities,
  calibration, recommended bench settings, the plug features `hil` uses and
  the Backlog/Delay finding, features it doesn't use yet). Keep it generic:
  no as-found state of a particular unit (addresses, readings, dates).

Commit messages use Conventional Commits with a scope, e.g.
`feat(hil): …`, `docs(nous-a8t): …`.
