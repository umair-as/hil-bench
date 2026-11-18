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
| MCP socket dir | `/run/hil-bench/` | `hil:hil-clients 2750` (agent users in `hil-clients` only) |

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

- **Agent: only through the MCP socket.** Run `hil mcp` as `hil` (e.g. a
  systemd service with `User=hil`) and give the agent access to its socket
  and nothing else:

  ```
  /run/hil-bench/            hil:hil-clients  2750   (setgid: new files get group hil-clients)
  /run/hil-bench/mcp.sock    created by: hil mcp --socket /run/hil-bench/mcp.sock --socket-mode 660
  ```

  Put the agent's user in a group of its own (`hil-clients` here). It must
  **not** be in `hil`, the group that owns the tty and the config. The MCP
  server forces the agent policy on every call and has no tool for
  `console`, `hold`, `uboot shell` or `--allow-destructive`. On Linux, a
  socket created in a setgid directory takes the directory's group, so
  `--socket-mode 660` opens it to `hil-clients` (check with `ls -l`).
- **Don't give the agent a sudo rule for the `hil` CLI.** Run as `hil`, the
  CLI does more than the policy covers: `hil hold -- CMD` runs any program
  as `hil` (it could read the credentials and open the tty), and
  `power watch --csv PATH` or `--log-dir DIR` write wherever `hil` can,
  including the action log. `caller = "agent"` doesn't refuse these.
- **Operator:** run the CLI as `hil` through your own sudo rule, or run `hil`
  as yourself with a config that sets the same `[paths]`. The operator's
  config shouldn't set `caller`.
- **Flashing under the lock** (`hil hold`) for an agent in this layout is
  still open. `hold` would have to run the command as the caller, not as
  `hil`, or offer only a fixed set of flasher commands.

## What this buys

Provided the MCP socket is the agent's only way in:

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
