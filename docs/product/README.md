# Product Evidence Layer

This directory is the product-management and user-evidence layer for Workbench.
It complements the engineering evidence chain; it does not replace the shared
runtime, Scenario Registry, Event Store, verifier, replay, or release gates.

## Operating principle

```text
user problem -> real task -> scenario contract -> implementation
             -> evidence -> verification -> product decision
```

Every proposed scenario should answer two separate questions:

1. Is this a real and repeated user problem?
2. Can the runtime prove the task outcome without confusing an action claim with observed state?

## Documents

- [Product brief](product-brief.md): current hypotheses, target users, jobs, non-goals, and validation plan.
- [90-day execution](90-day-plan.md): an evidence-gated operating cadence adapted from the product-manager plan.
- [Problem card template](problem-card-template.md): turn interviews into traceable problem evidence.
- [Design Partner scenario template](design-partner-scenario-template.md): define a bounded real-task collaboration.
- [Design Partner handoff boundary](design-partner-handoff.md): separate product acceptance, Motion/MCU/Safety authority, and evidence classes before a trial.
- [Feedback record template](feedback-record-template.md): make external installation and trial failures reproducible.
- [Metrics and decision log](metrics-and-decision-log.md): distinguish activity metrics from product and evidence outcomes.

## Evidence flow

Use the lightest artifact that preserves the decision trail:

1. **Problem card** records a user problem, quote, recent example, frequency, impact, and falsification condition.
2. **Design Partner scenario** records one bounded real task, responsibilities, safety boundary, and evidence agreement.
3. **Handoff boundary** records the allowed semantic interface, owner inputs/outputs, stop authority, and evidence class before scheduling the trial.
4. **Feedback record** records a reproducible installation, trial, or task result with version and run references.
5. **Engineering artifact** records the Scenario Registry entry, Event Store run, verifier result, and replay hash.
6. **Decision log** records whether to `continue`, `change`, `defer`, or `reject`, with links to the preceding evidence.

Do not skip from a conversation directly to a feature Issue. A feature becomes
ready only after its problem card, success condition, non-goals, and evidence
owner are clear. A scenario becomes release-relevant only after the applicable
engineering and evidence gates pass.

## Shared status vocabulary

| Status | Meaning | What it cannot mean |
|---|---|---|
| `hypothesis` | Team believes a problem or capability may matter | User validation |
| `observed` | One concrete user or run example exists | Repeated demand or general success |
| `repeated` | Independent evidence shows the same pattern | Physical safety or release readiness |
| `confirmed` | The declared evidence rule is satisfied for this claim | A stronger evidence class, such as physical validation |
| `insufficient_evidence` | Evidence is missing, stale, or conflicting | Success or failure by assumption |
| `failed` / `refuted` | The declared outcome or hypothesis was not met | Permanent product rejection without a decision |
| `not_executed` / `blocked` | The test did not run or cannot be completed | A successful result |

The same word must keep the same meaning in product records, run artifacts,
Dashboard views, and release reports. When in doubt, preserve the weaker status.

## Evidence and privacy boundary

The repository may contain only anonymized participant IDs, organization type,
scenario IDs, redacted problem statements, and evidence references. Do not commit
names, email addresses, phone numbers, budgets, private logs, raw videos, access
tokens, or identifiable customer material. Keep those in an access-controlled
private system and link only to an opaque evidence reference.

User statements, product hypotheses, scripted fixtures, Gazebo evidence, and
physical evidence are different evidence classes. None of them may silently
upgrade another class. In particular, a scripted fixture remains
`release_eligible: false` unless the applicable release gate says otherwise.
User feedback may motivate a product decision, but it is not execution or
physical evidence unless the applicable run and verification artifacts exist.

## Ownership boundary

- Product owns the problem definition, user evidence, prioritization, and acceptance intent.
- Runtime and architecture owners decide shared contracts and safety boundaries.
- Task/scenario owners implement domain rules without duplicating shared verification or replay.
- Motion, Perception, Navigation, MCU, and hardware owners implement adapters and physical validation.
- The human Project Owner owns Go/No-Go, scope, release, and external claims.

## Related execution Issues

- Multi-scenario epic: [#309](https://github.com/Quchaosheng/workbench-mobile-home-robot/issues/309)
- Contract and ownership: [#308](https://github.com/Quchaosheng/workbench-mobile-home-robot/issues/308)
- Definition of Ready: [#310](https://github.com/Quchaosheng/workbench-mobile-home-robot/issues/310)
- Capability matrix: [#311](https://github.com/Quchaosheng/workbench-mobile-home-robot/issues/311)
- Phase gates: [#314](https://github.com/Quchaosheng/workbench-mobile-home-robot/issues/314)

## Weekly review

The weekly product review should record only:

- new user evidence and the source reference;
- repeated problems and affected scenarios;
- decisions to start, change, defer, or reject work;
- external installation or task results;
- blocked evidence and the next owner/action/date.

Templates and verbal updates never move an engineering or release gate to green.
