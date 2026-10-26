# API reference

The versioned HTTP contract is checked in as
[`api-openapi-v1.json`](api-openapi-v1.json). `GET` is the only API method;
`POST`, `PUT`, `PATCH`, and `DELETE` return `405 read_only`. The service emits
`Content-Type: application/json; charset=utf-8`, `Cache-Control: no-store`,
`X-Content-Type-Options: nosniff`, and `X-API-Version: 1` on `/api/v1/*`.

`/api/*` is a compatibility alias for v1. Local and split-host clients use the
same `/api/v1` paths. Run and event responses are capped at 4 MiB; source files
are capped at 10 MiB and 10,000 events per run. Invalid run identifiers return
`400`, unknown runs return `404`, malformed sources return `503`, and oversized
responses return `413`. No endpoint writes, controls, or acknowledges a task.

`GET /api/v1/health` exposes the current validated health snapshot, the active
alerts derived from it, and the bounded history limits. `GET /api/v1/health/history`
returns the recent snapshots and cleared alerts, with an optional `since`
monotonic-second filter. Both are read-only: they cannot clear
an alarm, reset a fault, release contactors, or issue STOP. The backend
re-validates every metric against the fixed registry and recomputes the domain
and overall status, so a document cannot report `healthy` while carrying a
faulted, stale, missing, or conflicting critical metric. Missing data is
`unknown` or `degraded`, never healthy or zero. A malformed or oversized health
document returns `503 invalid_health_source` rather than a partial projection,
and an absent document reports `unknown` with `reason: no_health_snapshot`.

Both routes also carry `monitoring_policy`, the alert debounce and history policy
that instance actually runs: `open_after_s`, `clear_after_s`, `history_age_s`,
`max_history`, the `source` the values came from (`server-default`,
`environment`, `command-line` or `caller`), and a stable `sha256` over those
values. `debounce` is `debounce_disabled` only when both windows are zero, so an
unconfigured instance is labelled instead of looking like a deliberate zero.
The policy is validated before the socket is bound, and a value outside
`0..3600` seconds (or `0..86400` for `history_age_s`) stops startup with a usage
error rather than serving an alert path nobody approved.

The compatibility policy is additive within v1. A future v2 must be introduced
under `/api/v2`; aliases are not silently repointed.

The Python API pages are generated from source docstrings during the MkDocs build.

::: workbench.kernel.lifecycle

::: workbench.kernel.communication
