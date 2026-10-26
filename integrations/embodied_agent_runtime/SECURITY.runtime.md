# Security Policy

## Supported Code

Security fixes target the current `main` branch. Older commits and development
branches are not maintained as separate release lines.

## Reporting a Vulnerability

Do not open a public issue for a vulnerability or include credentials, private
network details, robot maps, device identifiers, or deployment logs in public
GitHub content.

Report vulnerabilities privately through a
[GitHub security advisory](https://github.com/Quchaosheng/embodied-agent-runtime/security/advisories/new).
Include the affected commit, the software-only reproduction steps, the likely
impact, and any suggested mitigation. Remove secrets and site-specific data
from attachments.

## Credential Handling

- Keep credentials in environment variables or an external secret store.
- Never commit `.env` files, private keys, access tokens, robot credentials, or
  private service URLs.
- Use narrowly scoped, revocable credentials for development and CI.
- Revoke and rotate a credential immediately if it appears in Git history,
  logs, screenshots, or an issue.

## Robot Safety Boundary

This repository demonstrates a deterministic ROS 2 workflow boundary. Its
validation, deadlines, cancellation handling, diagnostics, and software
`vcan0` tests are not a certified functional-safety system and do not replace
a hardware emergency stop, safety PLC, drive-level limits, guarded work area,
or deployment-specific risk assessment.

Treat every external request, perception observation, network message, and CAN
frame as untrusted. Physical camera, CAN, actuator, stopping, X5, BPU/NPU, and
GPIO behavior require target-specific validation before use around hardware.
