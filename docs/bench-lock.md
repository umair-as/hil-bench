# Bench lock protocol

`hil` serialises everything that changes a bench (power, boot runs, U-Boot
sessions, `hil hold`) with one lock file per bench. Another tool that
touches the same bench, such as a serial console server, a flasher or a CI
runner, can honour the same lock by following the rules below. Nothing in
this protocol is `hil`-specific apart from the path.

## Where the lock is

```
<state>/<bench>.lock
```

`<state>` is the config's `[paths] lock_dir`, else `[paths] state_dir`, else
`$XDG_STATE_HOME/hil-bench` (`~/.local/state/hil-bench` when
`XDG_STATE_HOME` isn't set). Every tool that shares a bench has to use the
same directory, including tools running as another user (see
[`service-user.md`](service-user.md)). Ask `hil` instead of re-deriving it:

```console
$ hil -b rpi5 lock
{"bench": "rpi5", "lock": "/var/lib/hil-bench/locks/rpi5.lock", "held": true,
 "holder_pids": [4242], "holder": "pid 4242 (boot) since 2026-01-01T12:00:00+00:00"}
```

`hil lock` only reads. It never takes the lock, even briefly.

## Taking the lock

1. Open the file, creating it if needed: `open(path, O_RDWR | O_CREAT, 0644)`.
   `O_RDONLY` is enough to lock if you can't write it.
2. Lock it with **`flock(fd, LOCK_EX | LOCK_NB)`**. It has to be `flock(2)`.
   On Linux, `fcntl`/POSIX record locks (`F_SETLK`, `lockf`) don't conflict
   with `flock` at all. A tool that uses them doesn't see `hil`'s lock, and
   `hil` doesn't see that tool's lock.
3. If it fails with `EWOULDBLOCK`, the bench is busy. Read the first line of
   the file for the holder and report it. Don't wait unless the operator
   asked you to; `hil` itself exits `3` immediately.
4. Once you hold it, and only if the file is writable: truncate it and write
   one holder line (format below).
5. Keep the fd open for as long as you need the bench. Closing it, or the
   process exiting, releases the lock. There is nothing to clean up.

Never delete, rename or replace the lock file. A process that opens a new
file at the same path locks a different inode, and two holders could then
run at once. The file stays after a run, so its holder line describes the
**last** holder. Only the flock says whether the bench is busy now.

## Holder line

```
pid <pid> (<what>) since <ISO 8601 timestamp with offset>
```

For example: `pid 4242 (uboot slot) since 2026-01-01T12:00:00+00:00`. `<what>` is
free text without a newline. Readers must treat the whole line as
informative text and must not parse it for decisions; `hil` truncates
`hold:` commands to 120 characters.

## Seeing who holds it

`/proc/locks` is world-readable and lists flocks by device and inode:

```
1: FLOCK  ADVISORY  WRITE 4242 fc:00:41307235 0 EOF
```

The fields are the pid, then `<major hex>:<minor hex>:<inode decimal>` of
the locked file. Lines with `->` after the index are blocked waiters. That
is how `hil lock` finds the holder without touching the lock, and it works
across users. The pid is the process that took the lock. Children that
inherited the fd hold it too.

## Nested use under `hil hold`

`hil hold -- CMD` passes its locked fd to CMD as `HIL_LOCK_FD=<n>`, with
`HIL_BENCH=<bench>`. A tool started under `hil hold` that would normally
take the lock can instead:

1. Check that `fstat(n)` matches `stat(<state>/<bench>.lock)` (same device
   and inode).
2. Call `flock(n, LOCK_EX | LOCK_NB)`. On the inherited descriptor it
   succeeds, because it's the same lock. An fd that came from a fresh
   `open()` fails while anyone else holds the lock, so the check can't be
   faked by opening the file.
3. If both pass, use the bench without truncating the file, rewriting the
   holder line, unlocking or closing `n`. The outer `hil hold` owns the lock.

## Limits

- The lock is advisory. It stops tools that follow this protocol, not a
  process that opens the tty or calls the plug directly. Making it binding
  takes file permissions: the lock directory, the tty and the plug
  credentials owned by a service user that the other callers can't
  impersonate.
- Anyone who can open the lock file can take the lock and keep it, which
  blocks the bench (a denial of service, not a takeover).
- `flock` is local to one machine. Don't put `<state>` on NFS.
