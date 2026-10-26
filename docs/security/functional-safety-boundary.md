# Standard positioning: what this repository claims, and against what

Status: **reference note, not a compliance statement.** No standard text is
reproduced here; only publicly described method is used, and the specific editions
in force must be confirmed with whoever owns the product's compliance paperwork.

This page exists because a repository that says "fail-closed STOP", "invariant" and
"assurance evidence" is easy to read as *functional safety*. It is not. Below is one
row per concept, stating what this repository actually does, so no reviewer has to
infer it.

## The one-paragraph answer

The repository is a **runtime** and a **runtime-assurance harness**. It can show that
a described rule holds under described conditions. It cannot show that a machine is
safe: there is no hazard identification for the product, no risk estimation, no
derived safety requirement, no performance level, and no physical evidence. Those are
the *first* steps of the standards below, and none has been performed.

## ISO 12100 — risk assessment and risk reduction

| The standard asks for | Here | Where it would live | Gap |
| --- | --- | --- | --- |
| Hazard identification | **No** (one worked example only) | `docs/deployment/hazard-analysis-cancel-then-stop.md` demonstrates the method on one function | the product's hazards are never enumerated |
| Risk estimation and evaluation | **No** | same document, recorded as `BLOCKED` | needs mass, speed, geometry, exposure, user population |
| Risk reduction, in the standard's order | **Partly** | stop-path design is fail-closed and tested | no safeguarding layer; no information-for-use layer beyond run evidence |
| Residual-risk statement | **No** | would be part of a machine-level file | no machine-level file exists |

The direction is the real gap. The invariants in
`docs/assurance/invariants-v1.json` were chosen **bottom-up**, from rules the code
already enforced. ISO 12100 runs **top-down**: hazards first, requirements derived
from them. Two documents cannot fix that; it needs the machine.

## ISO 13849-1 — safety-related control systems, design

| Concept | Here | Honest status |
| --- | --- | --- |
| Safety function definition | **No** | the invariants are runtime properties, not safety functions |
| Required performance level (PLr) | **No** | never assigned to anything |
| Architectural category | **No** | the stop path is not classified |
| MTTFd | **No** | no hardware reliability data exists |
| Diagnostic coverage | **No** | not measured |
| Systematic failure measures | **No** | none claimed |

The STOP path in
`services/agent_runtime/workbench_agent_runtime/execution_controller.py` is designed
to be fail-closed and is tested under fault. That is good engineering. It is not a
performance level, and deriving one from a software test suite is not possible.

## ISO 13849-2 — validation

Validation rests on a fault list, analysis of specified faults, and evidence against
systematic failures. This repository performs **fault injection as a test
technique** — see `docs/architecture/assurance-track.md`. That is *one kind of input*
such a validation could consume. It is not the validation:

- the fault list here was not derived from a hazard analysis, because there is none;
- no specified-fault analysis exists;
- the coverage class of every case is `SIMULATED`, which is not physical evidence.

## ISO 13482 — personal care robots

Applicability has **not been assessed**. Whether a mobile domestic robot with an arm
falls inside that standard's scope depends on intended use and machine
characteristics, and that judgement belongs with the product baseline recorded in
`docs/decisions/ADR-0007-household-mvp-baseline.md`. This repository takes no
position either way; that is a product decision, not an engineering one it can make.

## Sayable / not sayable

The left column is supported by a file, a symbol or a test in this repository. The
right column has no support here, and no amount of testing in this repository would
create it.

| Sayable | Not sayable |
| --- | --- |
| "the controller refuses to dispatch after STOP, and a test pins it" | "the STOP function is PL d" |
| "an ACK arriving after its command timed out cannot confirm that command" | "the safety function is validated" |
| "an unregistered scenario cannot start a run" | "hazards have been controlled" |
| "with missing evidence the verdict is `insufficient_evidence`, never a pass" | "the system is safe" |
| "fault injection covers these named cases at the simulated level" | "ISO 13849-2 validation performed" |
| "the reason for a terminal state is recorded and distinguishable" | "residual risk is acceptable" |
| "the mobile base and arm limits are outside this repository's evidence" | "the robot conforms to ISO 13482" |

A useful test for any future sentence: *can I point at a file and a test for it?* If
not, it belongs in the right column.

## What the assurance register does mean

Because the negative space above is large, it is worth stating the positive claim
precisely, since it is real:

- a claim is only admitted if its **enforcement point resolves** to a symbol and its
  **evidence resolves** to a test (`tools/scripts/check_assurance.py`);
- a fault case may never expect a `confirmed` verdict, so injection cannot become a
  completion;
- coverage never inflates: an executed case must cite a resolving test, and an
  unexecuted case must state its gap;
- gaps are printed rather than hidden — today, resource-claim exclusivity (INV-09) is
  `NOT_IMPLEMENTED` and says so on the generated page.

That is **runtime assurance and acceptance evidence**. It is a defensible engineering
claim. It is not a safety claim, and the distinction is the point of this page.

## If this work were taken further

The order is the opposite of the order this repository grew in:

1. ISO 12100 hazard identification for the actual machine and environment;
2. risk estimation, and a decision on which risks the control system must control;
3. those controls become **safety functions**, each with a required performance level;
4. only then is the stop path classified (category, MTTFd, DC) under ISO 13849-1,
   using hardware data that does not exist yet;
5. ISO 13849-2 validation, consuming the fault injection already here as one input.

Steps 1–3 are analysis and need no new hardware, so they are the honest next step.
Step 4 cannot be done by software at all.
