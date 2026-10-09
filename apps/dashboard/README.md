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

The run list shows each run's **scenario identity**, **verifier outcome** and
**evidence state** as three separate readings. The scenario label is `未登记场景`
when the run's legacy `task_id` has no registry manifest, and an outcome of `none`
reads `未验证` rather than borrowing a neighbouring success state. The two axes are
not synonyms: a run that recovered from a refutation still shows `未满足` under
证据 while its 结果 reads `已确认`, so the recovery stays visible.

The **场景/版本/结果/证据** selects are built from the `facets` the API reports, so
the UI cannot offer a filter value that would match nothing. Changing one reloads
the list from `/api/runs?<filters>&page=N&page_size=M` and the footer states
`第 X / Y 页 · 显示 N / M`, plus `全部 N` whenever a filter is active, so a page
of a filtered view can never read as the whole run set. An unknown filter value is
refused by the API with `400`; the view surfaces the error instead of silently
showing everything.

The 证据时间线 panel renders `GET /api/v1/runs/{run_id}/timeline`: every item keeps
its committed `sequence_no` order and exactly one phase — 执行, 观测, 验证, 恢复 or
上下文 — with a distinct left rule per phase. Context events are shown but are
never presented as verification. Until the timeline request succeeds the panel says
the timeline is not loaded, rather than reusing the event stream as if it had been
phase-checked.

The overview view is a **fixed two-column grid**: 事件时间线 and 证据时间线 stack in
the left column, 世界状态 and 安全与控制 in the right. Panels are grouped into exactly
two `.dashboard-column` stacks rather than placed as direct grid children, because
`grid-auto-flow` would otherwise push a third panel into an implicit second row and
leave the whole right-hand side of that row blank.

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

The trend table labels the domains the API reports (`应用`, `通信`, `计算`, `电源`,
`机器人`, `安全`, `任务`) and keeps the raw domain id as a tooltip. It cannot reuse
the card labels, because the cards split `communication` into `CAN 通信` and
`robot` into `定位`, `运动` and `感知`. Its **快照总体** column is the per-snapshot
domain roll-up, which is not the alert-aware **总体状态** shown above it; the
caption states the difference so two readings of "total" are not read as one
number. Every domain the API publishes has a card, so a reported metric is never
silently absent.

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
