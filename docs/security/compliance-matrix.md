# Security control readiness matrix

This is an internal readiness crosswalk, not a legal opinion, audit, certification, or claim of compliance with any named standard.

| Control objective | Repository evidence | State | External work still required |
|---|---|---|---|
| vulnerability reporting | `SECURITY.md` private advisory route and response targets | DEFINED | operate and measure response process |
| secure development review | secure review standard, CodeQL, tests, protected branch | PARTIAL | enable/require reviewed security checks after baseline PR |
| dependency governance | Dependabot, PR dependency review, SBOM, third-party review | PARTIAL | ongoing triage and license/legal approval |
| least privilege | read-only workflow default, job-scoped permissions, non-root container | IMPLEMENTED_BASELINE | deployment-specific IAM/network review |
| secret protection | secret scanning/push protection and documented handling | PARTIAL | validity checks, rotation drill, environment review |
| logging and evidence integrity | structured run/sequence logs, versioned redaction rules (`REDACTION_RULES_VERSION`) and evidence index | IMPLEMENTED_BASELINE | retention/access policy for deployment |
| incident response | roles, lifecycle, severity, and exercise design | DEFINED | tabletop and measured corrective actions |
| penetration testing | authorized scope, cases, stop conditions, evidence package | NOT_EXECUTED | independent authorized execution and retest |
| physical safety boundary | model/control separation, owner restrictions, fail-closed verification | PARTIAL | formal Gazebo and physical safety evidence |
| release provenance | tested image, SPDX SBOM, digest/provenance workflow | PARTIAL | successful next human-owned release proof |

Review this matrix at every phase gate and after material architecture, deployment, model, hardware, or regulatory changes.
