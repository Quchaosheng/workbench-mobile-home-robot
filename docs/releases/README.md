# Releases

The release surfaces answer different questions, and they are deliberately
separate rather than one page that guesses:

| Surface | Question it answers | Owner |
|---|---|---|
| [`phase-gates.md`](phase-gates.md) | Which multi-scenario delivery phase proves itself today, and what evidence class does it reach? | Release / QA |
| [`phase-status-v1.json`](phase-status-v1.json) | The machine-generated artifact behind that page, with the configuration hash it was built from | Release / QA |
| [`release-notes.md`](release-notes.md) | What this release states, linking the generated phase status rather than restating it | Release / QA |
| [`../evaluation/multi-scenario-readiness.md`](../evaluation/multi-scenario-readiness.md) | What the committed software is ready for, per registered scenario identity | Release / QA |

## Phase gates

Issue #314 splits the multi-scenario epic (#309) into four delivery phases and
asks for a status a release can link without hand-writing a claim. The status is
generated, never typed:

```bash
python3 tools/scripts/phase_gates.py --output docs/releases/phase-status-v1.json
python3 tools/scripts/check_phase_gates.py
python3 -m pytest tests/unit/test_phase_gates.py -v
```

A phase is marked `COMPLETE` only when every gate it declares currently passes.
A gate that answers `INCOMPLETE` holds the phase at `INCOMPLETE`, a gate that no
longer exists fails the phase, and a delivery probe that answers `NOT_DELIVERED`
marks the phase `BLOCKED` rather than failing the phases around it. The phase
status also copies each phase's `evidence_class` from the readiness report
(#307): with every registered scenario at `SCRIPTED_FIXTURE`, a phase can be
complete and still not release eligible, because software readiness is not a
physical capability. Every phase records the owner sign-off it still needs, and
no gate here can observe a human decision, so a sign-off is never reported as
made.

The gate compares the committed artifact against a regeneration, so a
hand-edited `gate_status`, a stale page, or a phase that stopped passing fails
`python3 tools/scripts/check_phase_gates.py`. The status grants no release
approval, replaces no required check and is not physical evidence.
