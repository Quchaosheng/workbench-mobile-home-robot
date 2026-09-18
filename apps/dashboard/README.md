# Dashboard (Owner: Interaction)

Read-only task status, evidence, replay, and robot-monitoring UI for Workbench-1.

```bash
python -m workbench_backend.server --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080`. A run can be deep-linked with `?run=<run_id>`; the parcel fixture uses `?run=dashboard-parcel--parcel-intake-003`. The backend serves committed fixture runs by default and can be pointed at any directory of ordered `.jsonl` event streams with `--data-dir`.

Parcel runs include a read-only decision table that shows the observed label and
condition, the policy-derived destination, and whether the actual placement
matches that decision.

Every run also shows an execution-claims table that keeps action results separate
from observed world state. A row reports the canonical `outcome`,
`dispatch_state`, and `device_state`, the location the action *claimed*, and the
location that was actually *observed*. Only an accepted `observation` event may
move an entity on the map; an action result never does. A claim reads
`awaiting observation` until the first observation of that entity after the
action, `unverified` when its window closed without one, `supported` when the
observation agrees, and `contradicted` when it disagrees. A claim is judged only
inside the window between its action result and that entity's next action result,
so a later placement supersedes an earlier hold instead of contradicting it.

The **机器人监控** tab renders `GET /api/v1/health` and
`GET /api/v1/health/history`: an overall status, active alerts sorted by severity
then age, per-domain freshness cards, and a bounded recent-trend table. It is
read-only and offers no acknowledge, reset, or stop control. A metric that was
never reported shows as `未上报` rather than `0` or "healthy", a stale metric
shows its age, and a failed refresh clears the cards instead of leaving a stale
green status. A card also defers to the backend alert rules, so a fresh reading
that still violates a threshold (a disk at zero free bytes) shows `降级` next to
its active alert instead of `正常`. Polling uses one bounded interval, backs off
after a failure, and stops when the tab or the page is hidden.

`data/health/health.jsonl` is a **simulation fixture**, labelled as such in the
view; the health document lives in a subdirectory because run logs are
discovered with a top-level `*.jsonl` glob and a sibling file would be parsed as
a run.

The HTTP boundary deliberately implements `GET` only. `POST`, `PUT`, `PATCH`, and `DELETE` return `405 read_only`; there is no ROS, MCU, motion, or emergency-stop publisher in this application.

## Concurrency and shutdown

The server bounds concurrent requests (`MAX_CONCURRENT_REQUESTS`) and refuses
work beyond that bound with `503 server_busy` plus `Retry-After`. On `SIGTERM` or
`Ctrl-C` it drains fail-closed in three ordered steps: `/readyz` reports
`not_ready`, every other route returns `503 server_draining` with `Retry-After`,
and in-flight requests are given `--drain-timeout` (default 5s,
`WORKBENCH_DRAIN_TIMEOUT_SECONDS`) to finish before the accept loop stops.
`/healthz` stays up for the duration of the drain, so an orchestrator can observe
NOT_READY and stop routing to the instance without killing it mid-request. A
handler that outlives the deadline is abandoned rather than allowed to block
process exit.

Vendored UI dependency: Lucide `0.468.0`, ISC license in `vendor/LUCIDE-LICENSE.txt`.

The dashboard follows a three-tab keyboard model: `Left`/`Right` (or `Up`/`Down`) changes views, while `Home` and `End` jump to the first or last view. Filters and run selection expose pressed state, replay exposes playback and position state, and the active mobile run scrolls into view. Nonessential motion is suppressed when the operating system requests reduced motion.
