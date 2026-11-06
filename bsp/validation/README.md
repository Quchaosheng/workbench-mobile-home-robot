# BSP Validation Evidence

Store raw bring-up captures outside source control when they contain sensitive
device details, and commit only immutable manifests and hashes.

Every record must identify board revision, kernel/JetPack version, firmware
versions, node IDs, instrument/calibration references, UTC timestamp, command
and result (`PASS`, `FAIL` or `NOT_EXECUTED`).

Minimum evidence set:

- Linux cold/warm boot and recovery transcript
- device-tree and interface enumeration
- CAN discovery and six-domain heartbeat capture
- STOP and independent E-stop timing capture
- per-domain reset and Linux restart behavior
- bus-off/restart counters and bounded queue behavior
- CPU, memory, temperature, power and CAN bus-load samples

`PASS` here means only that the named test ran and met its stated criterion. It
does not authorize production release; hardware release gates remain governed
by `hardware/release/`.

## Image build inputs

`bsp/image/build-inputs.yaml` is checked by
`bsp/validation/validate_image_inputs.py`. `build_ready: true` is accepted only
once every digest in the manifest has been verified against something real, so
a syntactically valid hex string cannot lock the manifest on its own.

- A local input (`kernel.config`, `device_tree.source`, `rootfs.services`,
  `rootfs.package_lock`, `recovery.image_source`) must resolve to a regular file
  inside the repository. The validator reads that file and recomputes its
  SHA-256; a mismatch reports the field name with the expected and actual
  digest. Missing, truncated, unreadable, directory and path-escape inputs fail
  closed.
- A network input (`jetpack.source`, `l4t.source`, `kernel.source`,
  `rootfs.base_source`) additionally requires a `retrieval` record with `url`,
  `size_bytes` and `sha256`. The URL must be HTTPS and match the declared
  source, the size must be a positive integer, and the recorded digest must
  equal the declared one. A bare 64-hex value is not a retrieval record.
- `toolchain.container_digest` needs the same `retrieval` record, with a
  registry reference pinned to that digest (`repo:tag@sha256:...`).
- Output digests (`outputs.*` and `device_tree.dtb_sha256`) must be either
  `NOT_BUILT` or bound to a `build_record`: `command`, a repository `log` and an
  `artifacts` map from digest field to repository file. Each artifact is hashed,
  so a stale or truncated output is reported under its field name.

Nothing is downloaded here. The validator only hashes files already in the
repository and compares recorded metadata, so an unverified vendor URL cannot
become a build claim. Keep `build_ready: false` and `status:
inputs_unresolved` until the artifacts and retrieval records actually exist.

