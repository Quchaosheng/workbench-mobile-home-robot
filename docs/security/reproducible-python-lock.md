# Reproducible Python lock

`pyproject.toml` declares ranges such as `pydantic>=2.8,<3`. A range is not a
reproducible input: it resolves to a different set of artifacts as the index
changes, and a hash cannot be attached to it. The root Python environment is
therefore installed from one committed, hash-checked lock:

```
docker/requirements-dev.lock
```

The lock is the resolved form of the `dependencies`, `dev` and `docs` ranges. It
pins every distribution as `name==version --hash=sha256:<hex>`, so an install
either uses the exact artifact that was reviewed or fails.

## Scope

| Environment | Input | Why |
|---|---|---|
| root Python runtime, `dev` and `docs` extras | `docker/requirements-dev.lock` | hashed, reproducible, installed in CI, Docker and `make bootstrap` |
| container-only MuJoCo capability | `requirements-mujoco.txt` | a separate capability environment with its own inputs and platform |
| ROS 2 and apt packages | `Dockerfile`, `docker/apt-packages.txt` | resolved by apt and rosdep, outside the Python lock |
| the project itself | `pip install --no-deps -e .` | an editable local project has no single artifact to hash |

The last row is the reason the install is two steps. pip refuses to combine
`--require-hashes` with an editable requirement, because there is no artifact to
hash:

```
ERROR: The editable requirement file:///... cannot be installed when requiring
hashes, because there is no single file to hash.
```

So the third-party set is installed from the lock with `--require-hashes`, and the
project is installed after it with `--no-deps`. `--no-deps` matters: without it,
pip would resolve the project's own requirements and defeat the lock.

## Installing

```bash
make bootstrap
```

which runs:

```bash
python3 -m pip install --upgrade pip
python3 -m pip install --require-hashes -r docker/requirements-dev.lock
python3 -m pip install --no-deps -e .
```

CI (`.github/workflows/ci.yml`) and the container build (`Dockerfile`) run the
same three steps in the same order, so the environment a developer sees is the
environment CI and the image see.

## Verifying

```bash
python3 tools/scripts/lock_python_dependencies.py verify   # offline
make lock-python-verify
```

Exit codes are the contract used by every gate in this repository:

| Code | Meaning |
|---|---|
| 0 | the lock is well-formed, covers every declared requirement, and every pin satisfies its range |
| 1 | the lock is readable and at least one rule is violated |
| 2 | the lock could not be judged: unreadable, or a header the tool does not understand |

`verify` never contacts an index. It refuses a line without a hash, a line that is
not `name==version`, a duplicate name at two versions, an empty lock, a missing
header field, an unknown schema, a lock resolved for another Python or platform,
a declared requirement with no pin, and an editable local project.

## Offline install from a populated cache

The lock is only useful if it reproduces without the index. With a directory of
the locked wheels already downloaded:

```bash
python3 -m pip download --only-binary=:all: --dest wheels -r docker/requirements-dev.lock
PIP_NO_INDEX=1 PIP_FIND_LINKS="$PWD/wheels" python3 -m pip install --require-hashes --target target -r docker/requirements-dev.lock
```

`PIP_NO_INDEX=1` is what makes this a real test: pip has no index to fall back on,
so a hash mismatch or a missing wheel fails instead of silently resolving.

## Detecting drift

```bash
python3 tools/scripts/lock_python_dependencies.py drift
```

This re-resolves the declared ranges and compares the result with the committed
lock. It reports, by name:

- a distribution that resolves but is not locked;
- a pin whose version has moved, so a rebuild would install something else;
- a pin the index no longer offers, such as a withdrawn release;
- a pin whose resolved artifact carries none of the locked hashes.

A drifted or yanked pin exits 1 rather than being accepted, and the run is
`INCOMPLETE` (2) when it cannot reach the index at all.

## Updating the lock

1. Change a range in `pyproject.toml`, or let a weekly Dependabot pull request
   propose the change.
2. Regenerate: `make lock-python`.
3. Run `make lock-python-verify` and `python3 tools/scripts/lock_python_dependencies.py drift`.
4. A human reviewer reads the lock diff, which shows exactly the pins that moved,
   checks upstream release notes, the license and the rollback path, and merges.

The lock never moves on its own. It is committed text; nothing regenerates it in
CI or at install time, and `verify` treats an unexpected change as a failure.

## Provenance

`tools/scripts/release_manifest.py` records the lock revision in the release
manifest: the lock path, the lock file's own sha256, the package count, the number
of packages carrying hashes, and every `name==version` with its resolved sha256
digests. `verify_manifest` refuses a revision whose package list does not match
its own count, or whose hashes are absent or malformed, so a manifest cannot claim
a lock it did not use.

See [supply-chain security](supply-chain.md) for how these inputs are reviewed,
and [release provenance](release-provenance.md) for the rest of the manifest.

## What this lock does not prove

A hash proves the artifact is the one that was resolved. It does not prove the
package is safe, correctly licensed for every use, or free of a known advisory.
Those remain review questions, answered from the upstream advisory and the
[triage process](supply-chain.md).
