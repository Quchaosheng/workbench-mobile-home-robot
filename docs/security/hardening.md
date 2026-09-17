# Hardening baseline

## Application and authority

- Dashboard endpoints remain `GET`-only; write methods fail closed.
- The dashboard has no ROS, MCU, motion, emergency-stop, secret, or release publisher.
- Models return a bounded route; trusted deterministic builders construct semantic actions.
- Remote or credentialed model endpoints are rejected by default.
- Verifier conclusions require evidence and remain separate from action/device status.

## Container and network

- Run as non-root UID `10001` with a read-only filesystem, `no-new-privileges`, and all Linux capabilities dropped.
- Bind the dashboard to localhost by default.
- Keep the optional model runtime on an internal network; only the bootstrap profile receives egress for explicit provisioning.
- Pin base and model images by digest; rebuild and rescan after updates.
- Use a small writable `tmpfs`; do not mount host control sockets or secret directories into the dashboard.

## Secrets and logs

- Use short-lived least-privilege GitHub/environment credentials and job-level permissions.
- Never place tokens in source, image layers, Compose files, CLI arguments, screenshots, fixtures, event logs, or artifacts.
- Structured logs retain run ID and monotonic sequence for investigation, but omit prompts/private payloads unless explicitly approved and access-controlled.
- Rotate an exposed credential before investigating convenience or blame.

### Redaction rule set

`workbench.application.redaction` is the single place that decides what may leave the
process. Its rule identity is `REDACTION_RULES_VERSION`, and every artifact it
scrubbed carries the version that produced it.

| Rule | What it catches |
|---|---|
| known credential formats | GitHub `ghp_`/`github_pat_` tokens, `sk-` model keys, Slack `xox`- tokens, AWS `AKIA`/`ASIA` key IDs, JWTs, PEM private-key blocks |
| credential headers | `Authorization`, `Proxy-Authorization`, `Cookie` and `Set-Cookie`, and `Bearer`/`Basic` schemes |
| credential URLs | `scheme://user:password@host` userinfo |
| credential names | credential-named mapping keys, query and form parameters, CLI flags, and environment assignments such as `--api-key`, `access_token=`, `client_secret` |
| raw evidence | byte payloads and raw prompt, frame, image, snapshot, video or recording fields |

Consumers apply it where untrusted text becomes an artifact:

- `workbench_backend.logging.StructuredLogger` scrubs the message, the nested
  `details` and exception text of every JSON Lines record. `emit_failure` builds
  the message inside the logger so a caller cannot copy a secret first.
- `tools/scripts/run_evaluation.py` scrubs the published `summary.json`, the
  runner timeout message and the failing-runner stderr message.

### What redaction must not change

- Correlation survives: `run_id`, `sequence_no`, `timestamp`, `event_id`,
  `action_id` and `evidence_refs` are copied through, so a scrubbed line can
  still be joined to its run and recomputed.
- A scrubbed record still has the same shape and stays valid JSON.
- A hash is the safe substitute for content: `prompt_sha256` and
  `snapshot_sha256` keep their value while `prompt` and `snapshot` do not.
  Counters such as `input_tokens` and `output_tokens` are counts, not credentials.

### Evidence by reference

Raw sensitive evidence - camera frames, recordings, prompts and model payloads -
is stored by reference, never copied into a public artifact. An artifact carries
`evidence_refs` plus a `<evidence-ref:redacted>` marker where the content used to
be. The referenced store is operator-owned and access-controlled; it is not a
repository path, not a CI artifact, and not reachable from the read-only
dashboard API. Raw inputs are retained under the deployment's retention and
access policy rather than inside the log line that cites them.

### Limits

- A secret with no known format and no credential-shaped name cannot be found in
  free text. Redaction is a last line of defense, never a reason to log a secret.
- Redaction is not authentication, authorization, or transport security. The
  deployment review below still applies.

## Deployment review

This baseline must be re-evaluated for TLS termination, authentication, reverse proxies, remote access, orchestration, host mounts, device access, and physical networks. The current localhost/offline assumptions do not authorize an internet-facing deployment.
