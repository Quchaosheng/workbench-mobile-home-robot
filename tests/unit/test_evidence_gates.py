import json
import unittest
from pathlib import Path

from validate_cold_start import validate

ROOT = Path(__file__).resolve().parents[2]


def participant(identifier: str, result: str = "pass", elapsed: float = 12, **overrides: object) -> dict:
    record = {
        "participant_id": identifier,
        "os": "Ubuntu 24.04",
        "cpu_memory": "8 vCPU / 16 GB",
        "docker_version": "29.0",
        "started_at": "2026-08-08T00:00:00Z",
        "first_health_at": "2026-08-08T00:05:00Z",
        "first_ready_at": "2026-08-08T00:12:00Z",
        "elapsed_minutes": elapsed,
        "result": result,
        "log_reference": f"runs/evaluation/{identifier}.log",
        "blocking_log_reference": f"runs/evaluation/{identifier}-blocked.log",
    }
    record.update(overrides)
    return record


def panel(*records: dict) -> dict:
    return {"protocol": "clean-machine-v1", "participants": list(records)}


class EvidenceGateTests(unittest.TestCase):
    def test_two_of_three_passes_is_accepted(self) -> None:
        summary = validate(panel(participant("one"), participant("two"), participant("three", "fail")))
        self.assertEqual(summary, {"participant_count": 3, "pass_count": 2, "accepted": True})

    def test_the_protocol_requires_exactly_three_participants(self) -> None:
        for count in (0, 1, 2, 4, 5):
            with self.subTest(participants=count), self.assertRaisesRegex(RuntimeError, "exactly 3"):
                validate(panel(*(participant(f"p{index}") for index in range(count))))

    def test_duplicate_participants_and_slow_passes_are_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unique"):
            validate(panel(participant("one"), participant("one"), participant("three")))
        with self.assertRaisesRegex(RuntimeError, "exceeded"):
            validate(panel(participant("one", elapsed=61), participant("two"), participant("three")))

    def test_every_documented_field_is_mandatory(self) -> None:
        fields = (
            "participant_id",
            "os",
            "cpu_memory",
            "docker_version",
            "started_at",
            "first_health_at",
            "first_ready_at",
        )
        for field in fields:
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, field):
                record = participant("one")
                record.pop(field)
                validate(panel(record, participant("two"), participant("three")))

    def test_placeholder_values_are_rejected(self) -> None:
        for placeholder in ("fill-in", "TBD", "n/a", "-", "  ", ""):
            with self.subTest(placeholder=placeholder), self.assertRaisesRegex(RuntimeError, "placeholder"):
                validate(
                    panel(
                        participant("one", os=placeholder),
                        participant("two"),
                        participant("three"),
                    )
                )

    def test_timestamps_must_parse_as_utc_and_be_ordered(self) -> None:
        cases = {
            "malformed": {"started_at": "sometime"},
            "timezone-naive": {"started_at": "2026-08-08T00:00:00"},
            "health before start": {"first_health_at": "2026-08-07T23:00:00Z"},
            "ready before health": {"first_ready_at": "2026-08-08T00:01:00Z"},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label), self.assertRaises(RuntimeError):
                validate(panel(participant("one", **overrides), participant("two"), participant("three")))

    def test_pass_records_need_a_log_and_a_bounded_elapsed_time(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "log_reference"):
            validate(panel(participant("one", log_reference="fill-in"), participant("two"), participant("three")))
        with self.assertRaisesRegex(RuntimeError, "exceeded"):
            validate(panel(participant("one", elapsed=60.5), participant("two"), participant("three")))
        # Exactly 60 minutes is still a pass.
        summary = validate(panel(participant("one", elapsed=60), participant("two"), participant("three")))
        self.assertEqual(summary["pass_count"], 3)

    def test_fail_records_need_a_blocking_log(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "blocking_log_reference"):
            validate(
                panel(
                    participant("one"),
                    participant("two"),
                    participant("three", "fail", blocking_log_reference="tbd"),
                )
            )
        summary = validate(panel(participant("one"), participant("two"), participant("three", "fail")))
        self.assertEqual(summary, {"participant_count": 3, "pass_count": 2, "accepted": True})

    def test_invalid_results_and_elapsed_values_are_rejected(self) -> None:
        for elapsed in ("12", True, float("nan"), float("inf"), -1):
            with self.subTest(elapsed=elapsed), self.assertRaisesRegex(RuntimeError, "elapsed_minutes"):
                validate(panel(participant("one", elapsed=elapsed), participant("two"), participant("three")))
        with self.assertRaisesRegex(RuntimeError, "result"):
            validate(panel(participant("one", result="maybe"), participant("two"), participant("three")))

    def test_malformed_documents_are_rejected(self) -> None:
        for payload in ({}, {"participants": {}}, {"participants": ["not-an-object"] * 3}, []):
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                validate(payload)

    def test_a_single_pass_is_not_accepted(self) -> None:
        summary = validate(panel(participant("one"), participant("two", "fail"), participant("three", "fail")))
        self.assertFalse(summary["accepted"])

    def test_the_checked_in_template_is_not_acceptable_evidence(self) -> None:
        template = json.loads(
            (ROOT / "docs" / "evaluation" / "cold-start-results.template.json").read_text(encoding="utf-8")
        )
        with self.assertRaisesRegex(RuntimeError, "placeholder"):
            validate(template)


if __name__ == "__main__":
    unittest.main()
