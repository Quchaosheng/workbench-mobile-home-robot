# OmniSim pilot identity v1

Issue: #455. Status: implemented. Supersedes the `uuid4` pilot identity.

The OmniSim pilot publishes one bounded probe of a vendor world. Before this
change it derived its own run identity with

```python
run_id = f"omnisim-{uuid.uuid4().hex[:12]}"
```

so the same world, the same step count and the same simulator configuration
produced a different run ID, and a different artifact directory, on every
invocation. Two reruns could not be compared as the same probe, and no field in
the artifact tied it to the world revision that was actually loaded.

## The identity input list

`integrations/omnisim/pilot.py` commits one ordered input list,
`IDENTITY_INPUTS`, and hashes it as strict JSON:

| Input | Value | Notes |
|---|---|---|
| `world_sha256` | the verified digest of the loaded world | Never the world path. A path is a name, not a revision. |
| `requested_steps` | `1`-`100` | The bounded step count the caller asked for. |
| `pilot_tool_version` | a committed constant | An identity input, so a pilot whose protocol handling changed cannot claim the old identity. |
| `pilot_identity_schema_version` | `omnisim-pilot-identity-v1` | Adding an input changes every identity; the schema version names the list. |

The run directory is `omnisim-<first 16 hex characters of the identity hash>`,
and `metadata.json` carries the whole identity block: the hash, the algorithm,
the input list in contract order and the material that produced it. Keeping the
material is deliberate, so a reviewer can see *which* input moved instead of
only seeing that two hashes differ. The identity is a pure function of these
inputs, so two processes given the same world digest and the same request agree
without sharing any state.

## The digest is verified, never inferred

The pilot refuses to invent provenance in three cases, and each one is an
`INVALID_OUTPUT` refusal rather than a fresh identity:

- **The harness reports no digest.** The load report and the capability report
  are both read; when neither carries one, the pilot fails closed instead of
  hashing the world path string and calling that provenance.
- **The harness reports two different digests.** The load report and the
  capability report must agree; an ambiguous answer is refused by name.
- **A digest disagrees with a source the pilot can check.** A caller may pin the
  digest it expects, and an absolute local world file must match the digest the
  harness says it loaded, so a stale local copy and a loaded world cannot
  silently diverge.

A refused pilot has no identity by construction, so it is recorded under the
constant directory name `omnisim-unidentified`. That name has a fixed cause, can
be compared across reruns, and deliberately does not have the
`omnisim-<hex>` shape, so it can never be mistaken for an evidence identity. The
collision rule below applies to it too: a second refusal into the same output
directory is refused rather than replacing the first one.

## Existing output is refused

The identity is now deterministic, so a second run into the same output
directory names the same directory. The pilot raises `FileExistsError` before it
creates a staging directory, so the first artifact, including its
`checksums.sha256`, is never replaced and no partial directory is left behind.
This is the same collision rule `sim_cli.py` already applies.

## Migration note

Artifact directory names change from `omnisim-<12 random hex>` to
`omnisim-<16 hex of the identity hash>`.

- **Existing pilot artifacts are not rewritten, renamed or re-hashed.** A
  directory whose name had the old random shape has no world digest recorded in
  its metadata, so it cannot be given a derived identity after the fact. It stays
  auditable as a diagnostic artifact and is not reproducible evidence.
- **Reruns of a pre-#455 pilot are not comparisons.** The old directories were
  produced under a different identity scheme; treat them as scratch runs rather
  than as the same probe as a new artifact.
- **A world with no harness digest cannot be piloted.** This is the intended
  incompatibility, not a bug: update the harness to report `world.digest` (or a
  top-level `digest`) on its load or capability response. The Issue's stop
  condition applies — report `NOT_EXECUTED` rather than hashing a path name.
- **`release_eligible` remains `false`.** The identity is reproducible, but a
  reproducible simulation probe is still simulation evidence: `evidence_class`
  stays `SIMULATION`, `physical_evidence` stays `false`, and
  `mapped_to_workbench_event_contract` stays `false`. No identity change
  promotes an OmniSim artifact past those claims.

## Owner approval

This change is scoped by Issue #455 and carries the `needs-owner-approval`
label. The human Simulation Integration owner must accept two decisions before
this can be evidence anyone else relies on:

1. **the identity input list** — `world_sha256`, `requested_steps`,
   `pilot_tool_version` and `pilot_identity_schema_version`, and nothing else;
2. **the directory migration** — that pre-#455 `omnisim-<random>` directories are
   retitled as non-reproducible diagnostic artifacts rather than migrated.

The Evaluation / Evidence reviewer must accept the third: that a missing harness
digest refuses the pilot as `INVALID_OUTPUT` instead of producing a best-effort
identity.
