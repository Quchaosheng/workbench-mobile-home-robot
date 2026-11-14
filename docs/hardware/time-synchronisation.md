# Time synchronisation on the target

Every wall-stamped evidence record depends on the board clock. This page says what
the repository does about it, and — more usefully — what it cannot do.

## Why this exists

A Jetson without a battery-backed RTC boots at a plausible wrong wall time. Nothing
loud happens: the kernel comes up, the services start, the monotonic clock is
correct, and every `occurred_at` written afterwards is wrong by a constant offset.

The repository did not notice, because the monotonic path is correct and the tests
inject their clocks. A wrong-by-hours evidence set looks exactly like a correct one
until someone compares it to an external reference.

## The service

`robot-time-sync` is declared in `bsp/rootfs/services.yaml` and has a unit template
at `bsp/rootfs/systemd/robot-time-sync.service.in`. It runs `chronyd` and is ordered
**before** `robot-evidence-logger`, so no record is stamped before the clock is
known.

Unlike the other services, it may change the system clock: the unit grants
`AmbientCapabilities=CAP_SYS_TIME`. It touches no bus and no actuator, so
`safety_authority` stays `false`.

`ExecStartPre=/usr/libexec/workbench/check-clock-plausibility` runs first. A clock
that predates the build date refuses the start rather than serving a wrong time.

## The two checks

`bsp/rootfs/clock_check.py` answers two questions, and both are reported rather than
assumed:

**Is the wall clock believable for this boot?** Two conditions:

1. the wall clock is not earlier than the configured build date — a clock that
   predates the software running on it was never set;
2. the implied boot time (`wall_now - uptime`) is not earlier than the build date
   either — a wall time further ahead than the uptime can explain means the clock
   was stepped forward after boot, which is exactly what a wrong RTC plus a later
   sync produces.

**Is the board clock within tolerance of a reference?** The bring-up step records
`clock_offset_seconds` beside the `timestamp_utc` and `raw_capture_hash`
`bsp/validation/bringup-plan.yaml` already requires, so a wrong clock is a recorded
number rather than an absence.

A refused clock does **not** discard the record. The record may be perfectly good
evidence about the device; it is the **timestamp** that is unusable, and the check
names which condition failed so a reader can use the record for what it supports.

## The container deployment cannot fix this

`deploy/multi-host/` runs the dashboard and the backend in containers. Those
containers **inherit the host clock**, and a container cannot repair a wrong host
time:

- a container shares the kernel clock, so it cannot step the host's time;
- granting `CAP_SYS_TIME` inside a container would change the host's clock from
  inside a workload, which is a privilege escalation an operator did not ask for;
- an NTP client inside the container would fight the host's, and the two would
  disagree visibly while both claiming to be right.

So there is deliberately **no** container-side time fix. If a deployment shows a
wrong wall time, the host is wrong and the host is where it must be fixed. Adding a
container-side correction would hide the problem on the one path where it is
easiest to observe.

## What this does not claim

- **No physical boot was performed.** The ordering and the units are verified
  statically and with `systemd-analyze verify`; that is a parse check, not a start
  check.
- **`observed_at` is unchanged.** This issue does not reinterpret it; the clock id
  vocabulary (`monotonic`, `wall`) is unchanged. Deciding what a wall-stamped record
  *means* when the clock was untrusted is a contract question for the world-model
  owner, not a BSP fix.
- **An RTC battery is a hardware question.** Whether the board has one decides
  whether a cold boot starts at a plausible-wrong time or an obviously-wrong one;
  [ADR-0011](../decisions/ADR-0011-stm32-mcu-selection.md) settles the MCU parts, not
  the RTC.
