import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from performance_tools import (
    MAX_CPU_PERCENT,
    MAX_DURATION_MS,
    MAX_MEMORY_BYTES,
    file_sha256,
    hardware_log_hashes,
    load_telemetry,
    parse_memory_bytes,
    software_environment,
    summarize_resource_samples,
    summarize_telemetry,
    validate_hardware_evidence,
    write_json_report,
)
from register_hardware_evidence import main as register_hardware_evidence


def record(source: str, run_id: str, sequence: int, stage: str, duration_ms: float) -> dict:
    return {
        "timestamp": "2026-08-08T00:00:00+00:00",
        "level": "INFO",
        "service": "test-pipeline",
        "source": source,
        "run_id": run_id,
        "sequence_no": sequence,
        "event": "stage_completed",
        "message": f"{stage} complete",
        "details": {"stage": stage, "duration_ms": duration_ms},
    }


class TelemetryTests(unittest.TestCase):
    def test_software_environment_has_comparison_identity(self) -> None:
        environment = software_environment()
        self.assertEqual(set(environment), {"platform", "python", "machine"})
        self.assertTrue(all(isinstance(value, str) and value for value in environment.values()))

    def test_simulation_and_hardware_use_the_same_aggregator(self) -> None:
        simulation = [
            record("simulation", f"sim-{index}", 0, "planning", value) for index, value in enumerate((1, 2, 9))
        ]
        report = summarize_telemetry(simulation)
        self.assertEqual(report["sources"]["simulation"]["stages"]["planning"]["p50_ms"], 2)
        self.assertEqual(report["sources"]["simulation"]["stages"]["planning"]["p95_ms"], 9)

        hardware = [record("hardware", "hw-1", 0, "planning", 4)]
        with self.assertRaisesRegex(RuntimeError, "operator evidence"):
            summarize_telemetry(hardware)
        attested = summarize_telemetry(
            hardware,
            hardware_evidence={"hardware_id": "arm-01", "operator": "tester", "captured_at": "now"},
        )
        self.assertTrue(attested["hardware_evidence"]["verified"])

    def test_loader_checks_sequence_and_hardware_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path = root / "hardware.jsonl"
            log_path.write_text(json.dumps(record("hardware", "hw-1", 0, "planning", 3)) + "\n", encoding="utf-8")
            records, paths = load_telemetry([log_path])
            self.assertEqual(len(records), 1)
            evidence_path = root / "evidence.json"
            evidence_path.write_text(
                json.dumps(
                    {
                        "evidence_kind": "operator_attested_real_hardware",
                        "hardware_id": "arm-01",
                        "operator": "tester",
                        "captured_at": "2026-08-08T00:00:00Z",
                        "logs": {log_path.name: file_sha256(log_path)},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(validate_hardware_evidence(paths, evidence_path)["hardware_id"], "arm-01")
            log_path.write_text(log_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "hashes"):
                validate_hardware_evidence(paths, evidence_path)

    def test_hardware_evidence_rejects_colliding_log_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first" / "run.jsonl"
            second = root / "second" / "run.jsonl"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "names must be unique"):
                hardware_log_hashes([first, second])

            evidence_path = root / "evidence.json"
            evidence_path.write_text(
                json.dumps(
                    {
                        "evidence_kind": "operator_attested_real_hardware",
                        "hardware_id": "arm-01",
                        "operator": "tester",
                        "captured_at": "2026-08-08T00:00:00Z",
                        "logs": {"run.jsonl": file_sha256(second)},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "names must be unique"):
                validate_hardware_evidence([first, second], evidence_path)

            output = root / "generated.json"
            with (
                patch(
                    "sys.argv",
                    [
                        "register_hardware_evidence.py",
                        str(first),
                        str(second),
                        "--hardware-id",
                        "arm-01",
                        "--operator",
                        "tester",
                        "--output",
                        str(output),
                    ],
                ),
                self.assertRaisesRegex(RuntimeError, "names must be unique"),
            ):
                register_hardware_evidence()
            self.assertFalse(output.exists())

    def test_resource_units_and_percentiles(self) -> None:
        self.assertEqual(parse_memory_bytes("1.5MiB"), 1572864)
        report = summarize_resource_samples(
            [
                {"Name": "dashboard", "CPUPerc": "1.00%", "MemUsage": "10MiB / 1GiB"},
                {"Name": "dashboard", "CPUPerc": "3.00%", "MemUsage": "12MiB / 1GiB"},
            ]
        )
        self.assertEqual(report["dashboard"]["cpu_percent_p95"], 3.0)
        self.assertEqual(report["dashboard"]["memory_bytes_max"], 12 * 1024 * 1024)


class TelemetryEvidenceBoundaryTests(unittest.TestCase):
    """Issue #107: malformed evidence must not become a percentile or a report."""

    def _write(self, root: Path, name: str, records: list[dict]) -> Path:
        path = root / name
        path.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")
        return path

    def test_non_finite_duration_is_rejected_by_the_aggregator(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(RuntimeError, "finite number"),
            ):
                summarize_telemetry([record("simulation", "run-1", 0, "planning", value)])

    def test_negative_and_absurd_durations_are_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "must not be negative"):
            summarize_telemetry([record("simulation", "run-1", 0, "planning", -600.0)])
        with self.assertRaisesRegex(RuntimeError, "must not exceed"):
            summarize_telemetry([record("simulation", "run-1", 0, "planning", MAX_DURATION_MS + 1)])

    def test_loader_rejects_non_finite_duration_before_aggregation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(
                Path(directory),
                "run.jsonl",
                [record("simulation", "run-1", 0, "planning", float("nan"))],
            )
            # json.dumps writes bare NaN, so the loader sees a non-finite literal.
            with self.assertRaisesRegex(RuntimeError, r"duration_ms.*run.jsonl:1"):
                load_telemetry([path])

    def test_loader_rejects_wrong_identity_types_with_path_and_line(self) -> None:
        cases = {
            "run_id": ("run_id", ["not", "hashable"]),
            "service": ("service", None),
            "timestamp": ("timestamp", 12345),
            "blank run_id": ("run_id", "   "),
        }
        for label, (field, value) in cases.items():
            with self.subTest(field=label), tempfile.TemporaryDirectory() as directory:
                payload = record("simulation", "run-1", 0, "planning", 1.0)
                payload[field] = value
                path = self._write(Path(directory), "run.jsonl", [payload])
                with self.assertRaisesRegex(RuntimeError, rf"telemetry {re.escape(field)}.*run.jsonl:1"):
                    load_telemetry([path])

    def test_negative_memory_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be negative"):
            parse_memory_bytes("-1MiB")

    def test_memory_above_the_bound_is_rejected(self) -> None:
        above = MAX_MEMORY_BYTES // (1024**3) + 1
        with self.assertRaisesRegex(ValueError, "above the"):
            parse_memory_bytes(f"{above}GiB")

    def test_resource_samples_reject_non_finite_and_negative_cpu(self) -> None:
        for value in ("NaN%", "-5%", "inf%"):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "CPU percent"):
                summarize_resource_samples([{"Name": "dashboard", "CPUPerc": value, "MemUsage": "1MiB / 1GiB"}])

    def test_resource_samples_accept_a_genuine_multi_core_reading(self) -> None:
        report = summarize_resource_samples([{"Name": "dashboard", "CPUPerc": "150.00%", "MemUsage": "1MiB / 1GiB"}])
        self.assertEqual(report["dashboard"]["cpu_percent_max"], 150.0)
        self.assertLess(150.0, MAX_CPU_PERCENT)

    def test_report_writer_refuses_non_finite_values_and_leaves_no_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            with self.assertRaises(ValueError):
                write_json_report(output, {"duration_ms": float("nan")})
            # Nothing half-written survives: the file was never created.
            self.assertFalse(output.exists())
            self.assertFalse((Path(directory) / ".report.json.tmp").exists())

    def test_report_writer_round_trips_valid_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "report.json"
            write_json_report(output, {"duration_ms": 12.5, "sources": {"simulation": 1}})

            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["duration_ms"], 12.5)
            self.assertFalse((output.parent / ".report.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
