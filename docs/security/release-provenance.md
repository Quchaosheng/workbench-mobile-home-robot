# Release provenance

A release image is only as trustworthy as the inputs we can name for it. This
page defines what a release manifest records, what makes a promotion refuse, and
why a mutable tag is never accepted as an artifact identity.

The rules are executable. `tools/scripts/release_manifest.py` writes the manifest
and decides whether it may be promoted, and
`tests/unit/test_release_provenance.py` exercises every refusal offline: image
inspection is injected, so no Docker daemon and no network are required.

## What the manifest records

| Field | Meaning |
|---|---|
| `source_commit` | Full 40-character commit the artifact was built from. |
| `workflow_ref` | The workflow ref that produced the artifact. |
| `base_image` | The `FROM` image with its resolved `sha256` digest and a `pinned` flag. |
| `lock` | The dependency-lock path, file hash, package count and exact `name==version` pins. |
| `sbom` | The SPDX SBOM path and its file hash. |
| `image` | The inspected image id, repo digests and the registry `sha256` digest. |
| `attestation` | `unsigned`, `signed` or `verified`, with the time it was recorded. |
| `release_eligible` | True only when the provenance above is complete. |

## Mutable tags cannot substitute for digests

A tag is a name that can resolve to different bytes tomorrow, so it identifies no
particular artifact. Only an `@sha256:` digest does. `parse_image_reference`
therefore reports a tag-only reference as unpinned rather than accepting it as a
weaker identity, and the base image recorded from the Dockerfile must carry a
digest.

Two consequences follow:

- A tag-only image has no registry digest, so `verify_manifest` refuses it. There
  is nothing immutable to promote and nothing to attest.
- A dependency range such as `pydantic>=2.8,<3` is not a lock entry. The lock must
  pin `name==version`, and a duplicate or unpinned line is refused rather than
  silently resolved again at build time.

## What refuses a promotion

`verify_manifest` returns the reasons a manifest must not be promoted, and
`promotion_reasons` applies the stricter question of whether it may be *published*.
A manifest is refused when any of the following is true:

- a required provenance field is missing;
- `source_commit` is not a full commit hash, or the version is not `v`-prefixed;
- the base image is not pinned to an immutable digest;
- the lock or SBOM hash is absent or not a hex digest;
- the image has no registry digest, which is the "mutable tag" case;
- the schema version is not the current one.

Provenance completeness and attestation are deliberately separate questions. The
build-time manifest is written before the attestation step exists, so it reports
`unsigned` truthfully and stays provenance-complete; the workflow then records
the attestation outcome with `--mark-attestation signed` and requires
`--require-promotion` before publishing. That keeps "we built this from known
inputs" distinct from "the published artifact carries an attestation".

## Where verification runs

In `.github/workflows/release-image.yml`:

1. the manifest is written with `--workflow-ref` from the environment;
2. `--verify release-manifest.json` runs before the image is attested, so an
   incomplete manifest stops the job before any attestation is produced;
3. after attestation, `--mark-attestation signed --require-promotion` records the
   outcome and refuses promotion when the manifest is incomplete;
4. the provenance artifact is uploaded with `if: always()`, so a failed run keeps
   the diagnostic record instead of publishing a partial artifact.

## Reproducibility

The manifest makes a rebuild checkable rather than claiming it is byte-identical.
Given the same commit, base-image digest and lock revision, the dependency and
base layers are reproduced; timestamps and the registry digest are recorded as
controlled nondeterminism instead of being asserted equal. The SBOM and lock
hashes are what let a reviewer confirm the rebuild used the same inputs.

See [supply-chain security](supply-chain.md) for how these inputs are reviewed and
updated, and [security hardening](hardening.md) for the wider boundary rules.

## What the tests prove

`tests/unit/test_release_provenance.py` covers a pinned versus tag-only image
reference, Dockerfile base-image resolution including a shadowing second `FROM`,
a fully pinned lock and the refusal of range, duplicate, empty and missing locks,
a complete manifest, the unsigned-but-provenance-complete case, each missing
field individually, the mutable-tag refusal, an unpinned base image, a short
commit, an unknown schema version, bad attestation values, deterministic
verification, attestation recording, the CLI exit codes, and the workflow
ordering that verifies provenance before publishing.
