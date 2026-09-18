# Task Packets

Every bounded AI-assisted or human task starts with an owner, allowed paths, acceptance commands and evidence. The foundation validates JSON packets today; the team may add YAML support after pinning a YAML parser.

An issue must satisfy the [Definition of Ready](../project-management/definition-of-ready.md)
before it is assigned, and a pull request must satisfy the
[Exit gate](../project-management/exit-gate.md) before it is reviewed. Both are read
from `tools/qa/governance/ready-gate-v1.json` and reproduced by
`python3 tools/scripts/check_ready_gate.py`.

With `--base`, the validator checks Git-visible changed paths against one packet. It is a consistency gate, not a filesystem sandbox: ignored build/runtime files are outside its scope, and human approval still comes from protected-branch review. Command checks recognize evidence-tool syntax but never execute or prove that an interpreter's arguments are safe.
