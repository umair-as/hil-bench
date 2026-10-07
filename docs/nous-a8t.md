# Nous A8T smart plug with Tasmota: notes for a HIL bench

The bench power switch `hil` drives is a Nous A8T plug running Tasmota and
controlled over its `/cm` HTTP API. These notes cover what the plug can do,
how to set one up for a bench, and how `hil` uses it.

## Hardware and firmware

| | |
|---|---|
| SoC | ESP32 (ESP32-U4WDH), 4 MB flash |
| Firmware | Tasmota, `tasmota32` build |
| Template | `Nous A8T` (BASE 1): Relay1, Button1, Led1i, BL0937-style pulse energy meter (`NRG_SEL_i`, `NRG_CF1`, `BL0937 CF`) |
| Scripting | **Berry** (on-device scripting) and **Rules** (3 × 511 B) |
| Network tools | **`Ping`**: the plug can ICMP-probe a host itself |
| MQTT | available in the build |

Security notes for any Tasmota plug on a bench:

- The web UI and the `/cm` API are plain HTTP. `/cm` takes the credentials
  as URL query parameters, so they cross the network in clear. Keep the
  plugs on an isolated bench network and don't reuse the plug password.
- The plug's admin credentials also allow changing rules, scripts, the OTA
  URL and `PowerOnState`. Treat them as admin access to the bench's power.
- Keep the firmware on a current release; check the configured `OtaUrl`
  before updating.

## Energy metering

- Reports W, VA, var, PF, V, A, and kWh totals (today, yesterday, total).
- It's a pulse-counting meter. Reading the relay state or energy values never
  touches the relay.
- `WattRes 0` means integer watts. A Pi 5 idles around 8 W, so ±1 W is a 12 %
  step; `WattRes 2` gives decimals, but true accuracy at low load is still
  about ±1 W.
- Uncalibrated plugs ship with template-default constants (`PowerCal 12530`,
  `VoltageCal 220`, `CurrentCal 3500`) and can read the mains voltage
  several percent high. **Calibrate against a known resistive load before
  quoting watts as data.** Firmware ≥ 14.2.0.1 uses the combined
  procedure: resistive load (incandescent/halogen, ≥ 60 W; not LED, motor or
  a switch-mode PSU, so neither the board nor a fan will do), `VoltRes 1`,
  `WattRes 2`, let it settle, then `VoltageSet <measured V>` followed by
  `PowerSet <load W>,<measured V>`, which sets power and current together.
  Check that PF reads about 1.00. Fine-tune with `PowerCal`/`CurrentCal`/
  `VoltageCal` (≤ 32000; needing more means the chip config is wrong).
  Calibrate each plug individually.
- Thresholds (`PowerHigh/Low`, `CurrentHigh`, `MaxPower`, `MaxEnergy`) exist
  and can trigger rules or an auto-off.

## Setting up a plug for a bench

| Setting | Tasmota default | For a bench plug | Why |
|---|---|---|---|
| `PowerOnState` | 3 (restore last) | 1 (always on) | an outage while the relay was off leaves the bench dark |
| Physical button | toggles relay | `SetOption73 1` (decouple) | a bump on the strip shouldn't power-cycle a test run |
| `Timezone` | fixed offset | 99 (use the DST rules) | otherwise plug time is off by an hour for half the year; matters for timers and logs |
| `WattRes` | 0 | 1–2 | resolution for low-power boards |
| `DeviceName`/`FriendlyName` | `Tasmota` | bench name | identical plugs are otherwise indistinguishable |
| `TelePeriod` | 300 s | 10–60 s if MQTT is used for power logging | |

A plug used for an appliance (not a bench) keeps its button coupled; only
`Timezone 99` and `WattRes` apply there.

## Plug features `hil` uses (opt-in)

Both are off unless a bench sets them; see README "Plug-side safety".

- **`PulseTime`** (`max_on_seconds`, `power on --max-on S`): the relay turns
  itself off S seconds after each switch-on, a dead-man for unattended runs.
  Encoding: 1–111 = 0.1 s steps, 112–64900 = value − 100 seconds. Checked
  on a bench plug: `--max-on 12` stored `112` and the relay switched off by
  itself about 12 s later, and `--max-on 0` cleared it again. The setting is
  stored on the plug.
- **`Backlog Power1 Off; Delay N; Power1 On`** (`cycle_on_plug`, used by
  `hil power cycle` only): a power cycle run by the plug itself. Checked on
  a bench plug: with `hil` killed (SIGKILL) mid-cycle, the plug still
  switched the board back on.

### Commands received during a `Backlog` `Delay` change the delay

Observed on Tasmota 14.3.0.1 (`tasmota32`). Each test was a
`Backlog Power1 Off; Delay 30; Power1 On` (3 s off) while the host queried
the relay state (`Power1`, no argument) over the `/cm` HTTP API:

| Host traffic during the delay | Result |
|---|---|
| none | relay back on after the 3 s delay, as expected |
| a query every ~0.25 s for the whole delay (0.05 s sleep, each request taking ~0.2 s) | delay stretched to ~6.3 s |
| quiet for 2.7 s, then a query every ~0.25 s | relay stayed off for > 15 s (two runs) |
| a query every 0.5 s | delay cut to ~0.6 s |

So any command the plug receives while a `Delay` runs disturbs it,
shortening or postponing it unpredictably. The exact cause in Tasmota's
backlog timer wasn't investigated. The status queries themselves were slow
in these runs: one took about 0.9 s to answer.

How `hil` avoids it:

- After sending the `Backlog`, `plug_side_cycle` sends nothing to the plug
  until the off time plus 1 s has passed. The UART flush that happens
  while the relay is open runs on the host only. Then it confirms ON with
  queries 0.5 s apart, and switches the relay on from the host only as a
  fallback.
- Without polling, the moment the relay closes isn't known precisely, so
  `boot` and `uboot` never use the plug-side cycle. They cycle from the
  host, which gives an exact t0 for stage timing.
- Anything else that talks to the plug during a plug-side cycle (another
  session's `power status`, a running `power watch`) still disturbs it.
  Don't overlap them.

## Bench-relevant features not yet used by `hil`

- **Rules + `Ping`**: an external watchdog that runs without the host. For
  example, ping the target every minute and power-cycle it after N misses.
- **`Matter`** (`SetOption151`): may or may not be built in; enabling it
  shows whether the `Mtr*` commands exist.
- **`Interlock`**: not applicable (single relay).
