# OmniSim pilot

This optional pilot talks only to an OmniSim World Harness on loopback. It
loads one vendor world in light mode, verifies a connected supervisor and a
finalized non-degraded Newton backend, restores the authored state, advances a
bounded number of steps, and records the raw vendor events in an atomic,
checksummed artifact.

```python
from pathlib import Path

from integrations.omnisim import OmniSimClient, OmniSimPilotRunner

result = OmniSimPilotRunner(OmniSimClient()).run(
    "projects/samples/demos/worlds/showcase/warehouse_husky.omniworld",
    Path("runs/omnisim"),
)
print(result.status, result.artifact_dir)
```

Start the separately installed simulator with `python -m omnisim harness`
before running the pilot. Every artifact is fixed to `evidence_class:
SIMULATION`, `physical_evidence: false`, `release_eligible: false`, and
`mapped_to_workbench_event_contract: false`.

## Run identity (Issue #455)

The run directory is `omnisim-<first 16 hex characters of the identity hash>`,
derived from the digest of the world the harness loaded plus the requested step
count, the pilot tool version and the identity schema version. The same world and
request therefore produce the same directory on every machine and in every
process, and `metadata.json` records the full identity block so a reviewer can
see which input moved when two probes differ.

The digest is verified rather than inferred. The harness must report the digest
of the world it loaded, at `world.digest` or a top-level `digest`, on its load or
capability response. A missing digest, two conflicting digests, a digest that
disagrees with an absolute local world file, or a digest that disagrees with a
caller-supplied `world_digest` is an `INVALID_OUTPUT` refusal. A refused pilot has
no identity, so it is published under the constant `omnisim-unidentified`
directory and carries `"identity": null`.

A rerun into the same output directory names the same directory, so the pilot
raises `FileExistsError` rather than overwrite the first artifact. See
`docs/architecture/omnisim-pilot-identity-v1.md` for the input list, the failure
rules and the migration note for pre-#455 random directories.

This adapter does not replace `tools/scripts/sim_cli.py`, translate Workbench
scenario manifests, or promote OmniSim events into the existing event-log
contract. Those require a later compatibility slice with explicit mapping,
reset-isolation, arm, gripper, camera, and collision tests.
