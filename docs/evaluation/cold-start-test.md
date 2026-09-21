# External cold-start protocol

Acceptance requires exactly three participant records and at least two of them reaching a healthy
dashboard within 60 minutes from a clean machine. Every field is mandatory: placeholder values such
as `fill-in` are rejected, timestamps must parse as UTC and satisfy `started_at <= first_health_at <=
first_ready_at`, a `pass` needs its `log_reference` and at most 60 minutes, and a `fail` needs its
`blocking_log_reference`.

## Participant records

Copy [`cold-start-results.template.json`](cold-start-results.template.json) to a private evidence file and fill it after each clean-machine run. Validate the completed file with:

```bash
python tools/scripts/validate_cold_start.py runs/evaluation/cold-start-results.json
```

The command intentionally fails until at least two of three real participants pass. The checked-in template is not evidence.

| Field | Value |
|---|---|
| Participant ID | |
| OS and version | |
| CPU / memory | |
| Docker version | |
| Started at | |
| First `/healthz` 200 at | |
| First `/readyz` 200 at | |
| Elapsed minutes | |
| Result | pass / fail |
| Log reference | required for `pass` |
| Blocking log reference | required for `fail` |

## Path under test

```bash
git clone https://github.com/Quchaosheng/workbench-mobile-home-robot.git
cd workbench-mobile-home-robot
docker compose up --build -d
curl --fail http://127.0.0.1:8080/healthz
curl --fail http://127.0.0.1:8080/readyz
```

Do not preinstall repository dependencies, reuse a prior image, or help the participant beyond the public README. Keep failures; they are release evidence, not scores to clean up.
