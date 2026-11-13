# Field support operating model

Issue 27 defines support from site acceptance through return, repair, and fleet
learning. It assumes remote-first diagnosis but never asks a customer to bypass a
guard, defeat an interlock, open a live enclosure, or handle a damaged battery.

## Intake record

Capture case ID, customer/site, contact, unit serial, hardware revision, software
and configuration hashes, deployment date, operating hours, symptoms, time first
seen, frequency, recent changes, environment, safety impact, photos/video, logs,
and actions already attempted. Preserve original timestamps and redact credentials
or personal data before attaching evidence to the engineering system.

## Severity and response

| Severity | Definition | Acknowledge | Action |
|---|---|---:|---|
| S0 | injury, smoke/fire, battery damage, uncontrolled motion, safety defeat | 15 min | stop use, isolate area if safe, emergency escalation |
| S1 | safety function unavailable or fleet-stopping defect | 1 h | contain affected units, incident lead, daily update |
| S2 | major function unavailable with safe workaround | 4 business h | diagnose, plan restore, track workaround |
| S3 | minor defect, question, cosmetic/documentation issue | 1 business day | normal queue and planned correction |

Targets are operating commitments only after staffing and contracts approve them.
For S0/S1, Support does not wait for full root cause before issuing containment.
Battery swelling, heat, odor, leakage, impact, or water ingress requires stop-use
and the supplier/emergency procedure; do not ship a damaged battery by normal courier.

## Diagnostic ladder

1. Confirm safety, identity, revision, power state, and reproducibility.
2. Export the supported diagnostic bundle and validate its checksum/time range.
3. Compare alarms, BMS state, power rails, temperatures, network, CAN, and services.
4. Reproduce with the same released configuration in a safe bench or simulation.
5. Apply only approved reversible recovery steps; record before/after evidence.
6. Escalate with a concise timeline, suspected subsystem, logs, and tested hypotheses.
7. Replace modules or authorize return only through serial-controlled procedures.

Remote access requires customer authorization, least privilege, session logging,
time-bounded credentials, and revocation at case closure. Support never requests
passwords in tickets. Logs and returned storage follow retention and privacy policy.

## Recovery, rollback, and return

Use an approved release artifact and verify signature/hash before update. Back up
configuration, record current versions, define success/abort criteria, keep stable
power, and never update during an unresolved power or thermal fault. Rollback is a
controlled release action, not an arbitrary package downgrade. After recovery,
repeat the affected acceptance tests and observe long enough to catch recurrence.

RMA authorization records unit and module serials, battery status, decontamination,
packaging, carrier restrictions, accessories, chain of custody, incoming inspection,
failure analysis, disposition, repair parts, outgoing test, and returned configuration.
No-fault-found units require improved diagnostics or reproduction evidence rather
than silent closure.

## Fleet learning and spares

Maintain critical spares for battery/power modules, compute, sensors, harnesses,
E-stops, and arm controllers based on installed base, lead time, observed failure,
and repair turnaround. Quarantine suspect lots. Weekly review groups cases by
confirmed root cause, revision, supplier lot, site, and hours; it feeds FMEA,
incoming inspection, test coverage, manuals, training, and release decisions.

Status: `PROCESS_DEFINED_STAFFING_REQUIRED`. Named on-call roles, contact channels,
spares quantities, site training, diagnostic tooling, privacy controls, and drill
evidence must be completed before production deployment.

Use `case-template.csv` for the minimum evidence record and
`escalation-matrix.csv` for response/containment ownership. The templates do not
contain customer data or claim staffing is already in place.

## Opening a case that carries its evidence

`support_case.py` is the bridge from a case to the artifacts a run produces. An S0
or S1 case opens an incident hold against its `run_id` and its `unit_serial` in the
same operation that creates the case, so a case cannot exist unbacked. A hold that
cannot be opened leaves no case behind either.

The hold outranks retention: `deletion_decision` in
`libs/application/workbench/application/data_governance.py` reports the hold as the
reason the run's event log, hardware record and release evidence cannot be deleted,
even after their retention window closes. S3 cases open no hold and say so.

Closing a case does **not** release its hold. Release is a separate action with an
author, a reason and a timestamp, because the moment an incident is over is not the
moment its evidence may be destroyed.

The case references evidence by the identifiers the runtime already uses, `run_id`
and `configuration_hash`, not by copied file paths, so a retention expiry is
detectable rather than silent. A case against a run identifier that does not exist
is refused at creation instead of being discovered missing mid-investigation.

This module does not change the retention table. `GOVERNANCE_RULES_VERSION` and
every `DataPolicy` value are untouched; a change to a retention value is its own
issue with the data owner.
