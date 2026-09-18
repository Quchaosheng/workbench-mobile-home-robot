# Project management baseline

This directory is the PMO operating layer for the 12-week P1/P2/P3 program. It coordinates work; it does not replace engineering evidence or approve a release.

## Evidence hierarchy

1. Immutable raw events, hardware records, and external participant records.
2. Deterministic validator output and GitHub Actions runs tied to a commit.
3. Generated reports that preserve blocked and not-executed states.
4. Status summaries and meeting notes that link to the evidence above.

Templates, scripted fixtures, estimates, and verbal updates never move a gate to green.

## Operating cadence

| Cadence | Input | Output | Accountable role |
|---|---|---|---|
| Daily | failed checks, new blockers, dependency changes | updated action and risk owners | Workstream owners |
| Weekly | milestone evidence, risk triggers, capacity | status update, decisions, escalations | Project Owner / PMO |
| Monthly | quality metrics, schedule variance, resource load | monthly report and reforecast | Project Owner |
| Phase gate | gate checklist and immutable evidence | Go, Pivot, or Stop decision | Human Project Owner |

## PMO task map

| Task | Repository artifact |
|---|---|
| PMO1 project plan and Gantt | [Project plan](plan.md) |
| PMO2 risk register | [`risks.csv`](risks.csv) |
| PMO3 weekly meeting framework | [Weekly meeting](weekly-meeting.md) |
| PMO4 progress dashboard | [Status dashboard](status.md) |
| PMO5 risk mitigation plan | [Risk management](risk-management.md) |
| PMO6 resource allocation | [Resource plan](resource-plan.md) |
| PMO7 monthly report | [Monthly report template](monthly-report-template.md) |
| PMO8 decision log | [Decision log](decision-log.md) |
| PMO9 lessons learned | [Lessons learned](lessons-learned.md) |
| PMO10 project closeout | [Closeout template](closeout-template.md) |
| PMO11 quality metrics | [Quality metrics](quality-metrics.md) |
| PMO12 later projects | [Future work](future-work.md) |
| PMO13 readiness and exit gates | [Definition of Ready](definition-of-ready.md) and [Exit gate](exit-gate.md) |

The [Definition of Ready](definition-of-ready.md) states what an issue body must
carry before it is assigned, and the [Exit gate](exit-gate.md) states what a pull
request body must carry before it is reviewed. Both are checked by
`python3 tools/scripts/check_ready_gate.py`, and neither grants merge, release or
physical-validation authority.

Update links and evidence before changing a status. The human Project Owner owns Go/No-Go, scope, release, and claims based on physical evidence.
