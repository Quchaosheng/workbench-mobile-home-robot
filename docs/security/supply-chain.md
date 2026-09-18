# Supply-chain security

## Controlled inputs

| Input | Control |
|---|---|
| Python dependencies | bounded version ranges in `pyproject.toml`, resolved into the hash-checked [reproducible Python lock](reproducible-python-lock.md), PR dependency review, weekly Dependabot updates |
| GitHub Actions | full 40-character commit pins with readable release comments |
| runtime/dev images | immutable digest pins and a shared-base regression test |
| model weights | separate source, license, redistribution, data, and hardware-fit review |
| vendored UI assets | local license record and versioned files |
| release image | tested candidate, SPDX JSON SBOM, registry digest, provenance manifest |

## Update process

1. Dependabot proposes a bounded update; it never merges or publishes.
2. Dependency review checks newly introduced vulnerabilities and fails on high/critical findings.
3. The owner reviews upstream release notes, source, license, transitive changes, and rollback path.
4. Required tests run against the exact pin or digest.
5. A human reviewer merges. A separate human-owned tag may publish only after release gates pass.

The root Python environment is installed from `docker/requirements-dev.lock`, which pins every
distribution to a sha256 digest; see [reproducible Python lock](reproducible-python-lock.md).
The release manifest that binds these inputs is defined in [release provenance](release-provenance.md).

SBOM generation inventories the built candidate. It does not prove a component is safe, licensed for every use, or present in a deployed physical unit. Store the SBOM and provenance with the release evidence index.

## Triage

For every alert, record affected package/path, advisory/CVE, reachability, exploit prerequisites, severity, fixed version, owner, decision, evidence, and deadline. Dismissal requires a reason and review date; absence from the runtime path should be demonstrated, not assumed.
