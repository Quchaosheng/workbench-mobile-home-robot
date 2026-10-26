# Robot monitoring view

The Dashboard has a third read-only tab, **机器人监控** (robot monitoring),
backed by the `GET /api/v1/health` and `GET /api/v1/health/history` endpoints
described in [the API reference](../api.md). It answers "is the robot well right
now", which is a different question from "did this task finish".

## What the view shows

- **Overall status** for the whole robot: `正常` (healthy), `降级` (degraded),
  `故障` (fault) or `未知` (unknown).
- **Active alerts**, sorted by severity and then by age, each with the metric,
  the severity (`提示`/`警告`/`严重`), the observation count, and the backend
  evidence reference.
- **Per-domain cards**: safety, power, CAN, compute, storage, localisation,
  motion, perception and task. Each metric shows its unit, its reporting source
  and how long ago it was observed.
- **Recent trend**: a bounded table of the last snapshots with the overall and
  per-domain status, so a fault and its recovery are visible without reading logs.

## What the view refuses to do

The monitoring view is strictly read-only. It cannot acknowledge an alarm, reset
a fault, release contactors, move the robot, or issue STOP. It has no buttons
that change robot state.

The view also refuses to guess:

- A metric the robot never reported shows as `未上报`, not as `正常` and not as
  `0`. An unhealthy `false` and an unknown value are different findings.
- A stale metric shows as `数据陈旧` with its age. A five-minute-old E-stop
  reading is not current evidence.
- If a refresh fails, the cards are cleared and the view reports that monitoring
  data is unavailable. It never leaves a stale green status on screen.
- A card defers to the backend alert rules. A disk with zero free bytes is a
  fresh, valid reading that still violates a threshold, so the storage card
  shows `降级` rather than `正常` while the alert is active.

## Fixtures versus hardware

The repository ships `apps/dashboard/data/health/health.jsonl`, a **simulation
fixture**. The view labels it plainly: *仿真夹具，未连接物理传感器*. Do not read
a fixture as evidence that the physical robot is healthy. Physical health
evidence requires real sensors and is recorded separately.

Monitoring polls the backend on one bounded interval while the tab is visible.
The interval backs off after a failure up to a ceiling, and polling stops
entirely when the page is hidden or another tab is selected, so an idle
Dashboard costs no requests.

## Reading an alert

| Field | Meaning |
| --- | --- |
| Severity | `提示` (info), `警告` (warning) or `严重` (critical) |
| Condition | The named failure, for example `estop_unavailable` or `can_bus_off` |
| Metric | The registry metric that produced the alert |
| Count | How many consecutive observations confirmed the condition |
| Evidence | A reference back to the health snapshot that supports it |

A critical condition is never hidden by another source reporting healthy. If a
subsystem is unreachable, the alert says the source is missing rather than
silently passing.

## Debounce policy

Debounce is server configuration, not a fixed constant. The backend CLI reads one
setting per window, and each falls back to the environment variable of the same
meaning:

| CLI option | Environment variable | Default | Bound |
| --- | --- | --- | --- |
| `--alert-open-after-s` | `WORKBENCH_ALERT_OPEN_AFTER_SECONDS` | `0` | `0..3600` |
| `--alert-clear-after-s` | `WORKBENCH_ALERT_CLEAR_AFTER_SECONDS` | `0` | `0..3600` |
| `--alert-history-age-s` | `WORKBENCH_ALERT_HISTORY_AGE_SECONDS` | `86400` | `0..86400` |
| `--alert-max-history` | `WORKBENCH_ALERT_MAX_HISTORY` | `512` | `1..512` |

With `open_after_s` set, a condition must persist that long before an alert
opens, so a single bad sample cannot page anyone. With `clear_after_s` set, a
condition must stay clear that long before the alert closes, so a single healthy
sample cannot clear a real fault. Either window may be set on its own.

The default is an explicit zero policy, and it is **labelled**:
`/api/v1/health` reports `"debounce": "debounce_disabled"`, so a deployment that
never configured debounce cannot be mistaken for one that chose it. Configuring
either window reports `"debounce": "debounce_enabled"` with the observed values.
The same block reports `source` (`server-default`, `environment`, `command-line`
or `caller`) and a `sha256` over the effective values, so two deployments can be
compared without reading each host's environment. It is also logged at startup
as `service_started.monitoring_policy`.

A malformed value stops startup before the port is bound: the CLI exits with
status 2 and names the offending setting. A blank value counts as unset, because
Compose interpolates an unset variable to the empty string. The concrete target
windows are an Operations/Safety decision; this repository documents the knob
and its bounds and deliberately ships no production threshold.
