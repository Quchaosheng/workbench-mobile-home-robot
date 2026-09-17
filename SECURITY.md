# Security policy

## Supported versions

Security fixes target the default branch and the latest published release. Older releases are unsupported unless the project owner explicitly names a backport. A published tag is not proof that hardware, simulation, or deployment safety gates passed.

## Report a vulnerability privately

Do not report vulnerabilities, credentials, private logs, or exploit details in a public Issue or pull request. Use [GitHub private vulnerability reporting](https://github.com/Quchaosheng/workbench-desk-robot/security/advisories/new). If that channel is unavailable, contact the project owner privately and include only enough information to establish a secure follow-up channel.

Include, when safe:

- affected commit, release, component, and configuration;
- prerequisites, minimal reproduction, and expected versus observed behavior;
- impact on confidentiality, integrity, availability, robot-control authority, or evidence integrity;
- known exploitation and suggested containment;
- whether any secret, personal data, physical device, or third party is involved.

Do not include live credentials. Redact tokens and private event data from screenshots and logs.

Structured logs, evaluation reports and model-call records are scrubbed by
`workbench.application.redaction` (`REDACTION_RULES_VERSION`). A record that was scrubbed
carries the rule version in its `redaction` field; a record without that field matched no rule.

## Response targets

These are response targets, not a bounty or guarantee:

| Stage | Target |
|---|---:|
| acknowledgement | 3 business days |
| initial severity and owner | 5 business days |
| containment plan for critical/high findings | 2 business days after triage |
| coordinated status update | at least every 7 days while open |

The human project owner makes disclosure, release, and risk-acceptance decisions. CI and AI tools cannot close a vulnerability or approve a safety claim.

## Security boundaries

- The public dashboard is read-only and must not publish ROS bridge, robot-control, firmware, emergency-stop, model-key, or private-log interfaces.
- Models may propose bounded semantic actions; they never receive joint, velocity, firmware, release, or completion authority.
- Physical task completion belongs to the independent verifier and requires evidence references.
- Default container execution is offline, non-root, read-only, capability-dropped, and separated from the optional model network.
- Secrets must use repository/environment secret stores and least-privilege short-lived tokens. They do not belong in source, images, fixtures, logs, or artifacts.

## Coordinated disclosure

Please allow time for containment, a reviewed fix, regression evidence, and affected-user guidance before public disclosure. The project will credit reporters who request credit and whose identity can be disclosed safely.
