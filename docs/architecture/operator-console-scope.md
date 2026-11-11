# Operator console: not in this deliverable

The control-plane surface an operator would need — cancel a running task, trigger
manual recovery, approve a re-attempt, act on reviewed evidence — is not part of
this release and has no implementation.

## Why this document exists

`apps/operator_console/` used to hold a README describing those capabilities and
`.gitkeep`. Nothing else: no entrypoint, no control boundary, no audit model, no
compose service, no test target. A directory that describes a capability it does
not have is worse than no directory, because it reads as a partially built
feature rather than an unstarted one.

The backend is deliberately read-only. The dashboard and the `/api/v1/*` surface
report state; they do not change it. A console that could cancel a task would need
a control boundary that does not exist yet, and adding one is a product decision
rather than an implementation detail.

## What a future console would have to settle first

- **A control boundary separate from the read-only API.** Writing on the same
  surface that bystanders read would make every read endpoint a potential
  actuator.
- **Authentication and authorisation.** The current API has neither, by design:
  it is a read-only projection. An operator action needs an identity to attribute
  it to.
- **An audit record per action.** Every cancel, recovery and approval has to
  produce evidence a reviewer can replay, in the same event vocabulary as the
  rest of the system.
- **A conflict model against the running agent.** Two operators, or one operator
  and the recovery ledger, must not both decide the next step.

None of those are small, and none are in scope for this release.

## What exists instead

- `apps/dashboard/` — read-only task status for bystanders.
- `/api/v1/health`, `/api/v1/monitoring`, `/api/runs/{id}` — read-only
  projections of health, monitoring and run state.
- `fault_injection` and the recovery ledger emit the events a future console would
  render; they deliberately do not accept commands.

Tracked as a future product item rather than an unfinished one.
