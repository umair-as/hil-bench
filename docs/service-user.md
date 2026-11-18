# Running `hil` as a service user

By default `hil` runs as whoever calls it. Then the caller can read the plug
credentials, take or delete the lock, edit the logs and open the tty
directly. Every guard in `hil` (bench lock, caller policy, denylist) is a
promise the caller keeps. Running `hil` as its own user turns those
promises into file permissions. This page describes that layout. Nothing in
`hil` creates users or changes permissions; set this up by hand.

## Layout

| What | Path (example) | Owner / mode |
|---|---|---|
| Service user | `hil` (no login shell) | |
| Bench config | `/etc/hil-bench/benches.toml` | `root:hil 0640` |
| Plug credentials | `/etc/hil-bench/tasmota.creds` | `hil:hil 0600` |
| State, action log | `/var/lib/hil-bench/` | `hil:hil 0750` |
| Locks | `/var/lib/hil-bench/locks/` | `hil:hil 0755` (others can read and flock) |
| Logs | `/var/log/hil-bench/` | `hil:hil 0755`, files `0644` (others read only) |
| Serial adapters | `/dev/serial/by-id/...` | group `hil`, `0660` via a udev rule |

`benches.toml` points at these locations:

```toml
[paths]
state_dir  = "/var/lib/hil-bench"          # actions.jsonl
lock_dir   = "/var/lib/hil-bench/locks"    # <bench>.lock (default: state_dir)
log_dir    = "/var/log/hil-bench"          # default --log-dir
creds_file = "/etc/hil-bench/tasmota.creds"  # for benches without their own
```

Paths must be absolute (`~` is expanded), and unknown keys are refused, so
a typo can't silently fall back to a per-user location. Every tool that
shares a bench must use the same lock directory (see
[`bench-lock.md`](bench-lock.md)).

## Calling it

- **Agent:** a sudoers rule lets the agent's user run only `hil`, as `hil`,
  with a clean environment:

  ```
  agent ALL=(hil) NOPASSWD: /usr/local/bin/hil
  Defaults>hil env_reset
  ```

  With `env_reset`, the agent can't pass `HIL_BENCH_CONFIG`,
  `HIL_LOCK_FD` or `TASMOTA_*`. Put `caller = "agent"` at the top of the
  service config, so every call through this rule gets the agent policy.
- **Operator:** a second config readable only by the operator and `hil`
  that sets the same `[paths]` without `caller`, used through a separate
  rule (`sudo -u hil env HIL_BENCH_CONFIG=/etc/hil-bench/operator.toml hil`),
  or `hil` run directly as the operator for maintenance.
- **A later MCP server** runs as `hil` too and is reached over a Unix
  socket whose permissions decide who may connect.

## What this buys

- The agent can't read the plug credentials, which are plug admin
  (rules, scripts, OTA URL, `PowerOnState`), so it can't call the plug's
  API around `hil`. Also keep the plugs on a bench network the agent's host
  can't route to: the plug API takes credentials in the URL over plain
  HTTP.
- The agent can't delete or rewrite locks, the action log or boot logs.
- With the tty group-owned by `hil`, only `hil` (and whatever else is in
  that group) can open the UART.

## Port check across users

Before opening a tty, `hil` looks for other users of it in three ways:

1. `/proc/<pid>/fd` of every process it may read. This sees any opener
   running as the same user.
2. `/proc/locks`, which is world-readable. This sees a `flock(2)` on the
   tty from any user, which is what `tio` and pyserial's `exclusive=True`
   take.
3. The open itself: a tty another process put in `TIOCEXCL` mode fails with
   `EBUSY`, and `hil` reports it as held exclusively.

A process of another user that opens the tty with neither a flock nor
`TIOCEXCL` stays invisible without root. The tty permissions above close
that gap.
