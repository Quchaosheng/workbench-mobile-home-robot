import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "scripts"))

from _jsonio import JsonInputError, load_jsonl, loads
from collect_metrics import (
    audit_false_completions,
    collect,
    duration_sources,
    durations,
    load_runs,
    run_metadata,
)
from compare_evaluations import compare, wilson_interval
from generate_report import load as load_metrics
from generate_report import release_reasons
from run_evaluation import (
    EXTERNAL_STARTUP_GRACE_SECONDS,
    EvaluationInputError,
    ExternalRunnerTimeout,
    external_timeout_budget,
    load_scenario_manifests,
    run_external,
    scripted_events,
    validate_event_log,
    validate_label,
    write_jsonl,
)
from scenario_tools import canonical_hash, materialize_scenario, validate_simulation_manifest
from validate_golden_set import validate, validate_diverse, validate_parcels


class ScenarioToolTests(unittest.TestCase):
    def test_same_seed_produces_same_scene_hash(self) -> None:
        manifest = json.loads((ROOT / "sim" / "scenarios" / "frozen" / "normal-001.json").read_text())
        first = materialize_scenario(manifest)
        second = materialize_scenario(manifest)
        self.assertEqual(first, second)
        self.assertEqual(canonical_hash(first), canonical_hash(second))

    def test_golden_set_counts_and_fail_closed_policy(self) -> None:
        payload = json.loads((ROOT / "evaluation" / "golden-set-v0.1.json").read_text(encoding="utf-8"))
        self.assertEqual(validate(payload), [])
        diverse = json.loads((ROOT / "evaluation" / "golden-set-v0.2.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_diverse(diverse), [])
        parcels = json.loads((ROOT / "evaluation" / "golden-set-parcel-v0.1.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_parcels(parcels), [])


class EvaluationPipelineTests(unittest.TestCase):
    def test_scripted_log_has_metadata_sequence_and_verification_evidence(self) -> None:
        manifest = json.loads(
            (ROOT / "sim" / "scenarios" / "frozen" / "occlusion-001.json").read_text(encoding="utf-8")
        )
        events = scripted_events("v-test", manifest, "abc123", 1000)
        self.assertEqual([event["sequence_no"] for event in events], list(range(len(events))))
        self.assertTrue(all(event["evaluation"]["seed"] == 1104 for event in events))
        verification = [event for event in events if event["event_type"] == "verification"][-1]
        self.assertEqual(verification["payload"]["status"], "insufficient_evidence")
        self.assertTrue(verification["payload"]["evidence_refs"])
        self.assertEqual(
            verification["payload"]["evaluated_conditions"],
            verification["payload"]["required_conditions"],
        )
        self.assertNotEqual(
            verification["payload"]["satisfied_conditions"],
            verification["payload"]["required_conditions"],
        )

    def test_multi_object_scenario_generates_three_entity_task_evidence(self) -> None:
        manifest = json.loads(
            (ROOT / "sim" / "scenarios" / "expanded" / "multi-object-003.json").read_text(encoding="utf-8")
        )
        events = scripted_events("v-test", manifest, "abc123", 1000)
        observations = [event for event in events if event["event_type"] == "observation"]
        self.assertEqual(
            {event["payload"]["entity_id"] for event in observations},
            {"red_block", "blue_cylinder", "green_gear"},
        )
        final = [event for event in events if event["event_type"] == "verification"][-1]
        self.assertEqual(final["payload"]["task_id"], "task-kit-three-parts")
        self.assertEqual(len(final["payload"]["required_conditions"]), 4)

    def test_parcel_scenario_carries_per_entity_attributes_and_routes(self) -> None:
        manifest = json.loads(
            (ROOT / "sim" / "scenarios" / "expanded" / "parcel-intake-003.json").read_text(encoding="utf-8")
        )
        scene = materialize_scenario(manifest)
        self.assertEqual(scene["task_id"], "task-sort-parcels")
        self.assertEqual(len(scene["objects"]), 3)
        self.assertEqual(
            next(item for item in scene["objects"] if item["entity_id"] == "parcel_damaged")["attributes"],
            {"label_status": "verified", "condition": "damaged", "barcode": "WBX-DMG-20260807"},
        )
        self.assertEqual(
            next(item for item in scene["objects"] if item["entity_id"] == "parcel_unreadable")["attributes"],
            {"label_status": "unreadable", "condition": "intact", "parcel_uid": "WBX-UNK-20260807"},
        )
        events = scripted_events("v-test", manifest, "abc123", 1000)
        observations = [event for event in events if event["event_type"] == "observation"]
        observe_requests = [event for event in events if event["event_type"] == "action_request"]
        graph = next(event for event in events if event["event_type"] == "task_graph")["payload"]
        self.assertEqual(graph["planner"], "parcel-policy-v3")
        self.assertTrue(graph["observation_barrier"])
        self.assertTrue(graph["manipulation_serial"])
        self.assertEqual(graph["routing_policy"], "manifest_matched_verified_intact_only")
        self.assertEqual(graph["policy_version"], "parcel-routing-v3")
        self.assertEqual(graph["manifest_id"], "WB-INBOUND-20260807-003")
        self.assertEqual(set(graph["manifest_statuses"].values()), {"matched"})
        self.assertEqual(graph["destination_capacities"], {"pickup_shelf": 4, "quarantine_bin": 4})
        self.assertEqual(graph["destination_occupancy"], {"pickup_shelf": 0, "quarantine_bin": 0})
        self.assertEqual(
            graph["routing_priorities"],
            {
                "parcel_box": "standard",
                "parcel_unreadable": "label_exception",
                "parcel_damaged": "condition_exception",
            },
        )
        self.assertEqual(
            graph["actions"][0:3],
            [
                "observe:parcel_box",
                "observe:parcel_unreadable",
                "observe:parcel_damaged",
            ],
        )
        self.assertEqual(graph["actions"][3], "grasp:parcel_damaged")
        self.assertTrue(all(event["payload"]["attributes"] for event in observations))
        self.assertTrue(
            all(
                event["payload"]["attributes"] == ["label_status", "condition", "tracking_id", "barcode", "parcel_uid"]
                for event in observe_requests[:3]
            )
        )
        parcel_place_request = next(
            event
            for event in observe_requests
            if event["payload"].get("action_type") == "place" and event["payload"].get("target_id") == "parcel_damaged"
        )
        self.assertEqual(parcel_place_request["payload"]["identity_guard"], "unique_across_supported_fields")
        self.assertEqual(parcel_place_request["payload"]["manifest_guard"], "matched")
        self.assertEqual(parcel_place_request["payload"]["manifest_id"], "WB-INBOUND-20260807-003")
        self.assertEqual(parcel_place_request["payload"]["routing_priority"], "condition_exception")
        self.assertEqual(parcel_place_request["payload"]["destination_remaining_after"], 3)
        destinations = {
            event["payload"]["resulting_location"]
            for event in events
            if event["event_type"] == "action_result" and event["payload"].get("resulting_location")
        }
        self.assertEqual(destinations, {"in:pickup_shelf", "in:quarantine_bin"})

    def test_metrics_keep_unaudited_false_completion_unknown(self) -> None:
        manifest = json.loads(
            (ROOT / "sim" / "scenarios" / "frozen" / "occlusion-001.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            version_dir = root / "v-test"
            version_dir.mkdir()
            events = scripted_events("v-test", manifest, "abc123", 1000)
            log_path = version_dir / "occlusion-001.jsonl"
            write_jsonl(log_path, events)
            validate_event_log(log_path, "v-test--occlusion-001")
            (root / "summary.json").write_text(
                json.dumps({"runner": "scripted", "release_eligible": False}),
                encoding="utf-8",
            )
            metrics = collect(version_dir)
        self.assertIsNone(metrics["false_completion_count"])
        self.assertFalse(metrics["release_eligible"])
        self.assertEqual(metrics["evidence_coverage"], 1.0)
        self.assertEqual(metrics["task_family_count"], 1)
        self.assertEqual(metrics["complex_task_rate"], 0.0)
        self.assertEqual(metrics["goal_condition_coverage"], 1.0)

    def test_metrics_expose_task_diversity_and_multi_entity_complexity(self) -> None:
        manifests = [
            json.loads((ROOT / "sim" / "scenarios" / "frozen" / "normal-001.json").read_text(encoding="utf-8")),
            json.loads((ROOT / "sim" / "scenarios" / "expanded" / "multi-object-003.json").read_text(encoding="utf-8")),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            version_dir = root / "v-test"
            version_dir.mkdir()
            for manifest in manifests:
                write_jsonl(
                    version_dir / f"{manifest['scenario_id']}.jsonl",
                    scripted_events("v-test", manifest, "abc123", 1000),
                )
            (root / "summary.json").write_text(
                json.dumps({"runner": "scripted", "release_eligible": False}),
                encoding="utf-8",
            )
            metrics = collect(version_dir)
        self.assertEqual(metrics["task_family_count"], 2)
        self.assertEqual(metrics["complex_task_rate"], 0.5)
        self.assertEqual(metrics["mean_observed_entities"], 2.0)
        self.assertEqual(metrics["task_family_distribution"]["task-kit-three-parts"], 1)

    def test_duplicate_or_unsafe_run_inputs_are_rejected_before_execution(self) -> None:
        manifest = json.loads((ROOT / "sim" / "scenarios" / "frozen" / "normal-001.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text(json.dumps(manifest), encoding="utf-8")
            second.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(EvaluationInputError, "duplicate scenario_id"):
                load_scenario_manifests([first, second])
        with self.assertRaisesRegex(EvaluationInputError, "filesystem-safe"):
            validate_label("../outside", "version")

    def test_evaluation_manifest_adapter_accepts_reviewed_extension_only(self) -> None:
        expanded = ROOT / "sim" / "scenarios" / "expanded" / "multi-object-003.json"
        loaded = load_scenario_manifests([expanded])
        self.assertEqual(loaded[0][1]["scene_variant"], "multi_object")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "unknown-extension.json"
            payload = dict(loaded[0][1])
            payload["unreviewed_extension"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(EvaluationInputError, "unknown simulation manifest fields"):
                load_scenario_manifests([path])

    def test_standalone_and_runner_manifest_validation_agree_on_noncanonical_types(self) -> None:
        manifest = json.loads((ROOT / "sim" / "scenarios" / "frozen" / "normal-001.json").read_text(encoding="utf-8"))
        coercible = {
            **manifest,
            "seed": str(manifest["seed"]),
            "timeout_s": str(manifest["timeout_s"]),
            "oracle_allowed": "false",
        }
        # The standalone validator must not coerce values the runner rejects.
        with self.assertRaisesRegex(ValueError, "seed"):
            validate_simulation_manifest(coercible)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "coercible.json"
            path.write_text(json.dumps(coercible), encoding="utf-8")
            with self.assertRaisesRegex(EvaluationInputError, "invalid scenario manifest"):
                load_scenario_manifests([path])

        # Unknown fields stay rejected by both entrypoints, and the reviewed
        # scene_variant extension is still accepted.
        with self.assertRaisesRegex(ValueError, "unknown simulation manifest fields"):
            validate_simulation_manifest({**manifest, "unreviewed_extension": True})
        self.assertEqual(validate_simulation_manifest(manifest).scenario_id, manifest["scenario_id"])

    def test_event_log_rejects_bad_json_missing_verification_and_boolean_sequence(self) -> None:
        manifest = json.loads((ROOT / "sim" / "scenarios" / "frozen" / "normal-001.json").read_text(encoding="utf-8"))
        events = scripted_events("v-test", manifest, "abc123", 1000)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "events.jsonl"
            path.write_text("{bad-json}\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unreadable JSONL"):
                validate_event_log(path, "v-test--normal-001")

            without_verification = [event for event in events if event["event_type"] != "verification"]
            for index, event in enumerate(without_verification):
                event["sequence_no"] = index
            write_jsonl(path, without_verification)
            with self.assertRaisesRegex(RuntimeError, "no verification"):
                validate_event_log(path, "v-test--normal-001")

            events[0]["sequence_no"] = False
            write_jsonl(path, events)
            with self.assertRaisesRegex(RuntimeError, "non-contiguous"):
                validate_event_log(path, "v-test--normal-001")

            for index, event in enumerate(events):
                event["sequence_no"] = index
            events[0]["event_type"] = []
            write_jsonl(path, events)
            with self.assertRaisesRegex(RuntimeError, "unknown event_type"):
                validate_event_log(path, "v-test--normal-001")

    def test_event_metadata_must_match_the_requested_scenario(self) -> None:
        manifest = json.loads((ROOT / "sim" / "scenarios" / "frozen" / "normal-001.json").read_text(encoding="utf-8"))
        events = scripted_events("v-test", manifest, "abc123", 1000)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "events.jsonl"
            write_jsonl(path, events)
            with self.assertRaisesRegex(RuntimeError, "seed drift"):
                validate_event_log(
                    path,
                    "v-test--normal-001",
                    scenario_id="normal-001",
                    seed=999,
                    commit="abc123",
                )

    def test_duplicate_json_object_keys_are_rejected_at_every_evidence_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            manifest = root / "manifest.json"
            manifest.write_text('{"scenario_id": "a", "seed": 1, "seed": 2}', encoding="utf-8")
            with self.assertRaisesRegex(EvaluationInputError, "duplicate JSON object key: 'seed'"):
                load_scenario_manifests([manifest])

            log = root / "events.jsonl"
            log.write_text('{"run_id": "attacker", "run_id": "v-test--normal-001"}\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "duplicate JSON object key: 'run_id'"):
                validate_event_log(log, "v-test--normal-001")

            scenario = json.loads(
                (ROOT / "sim" / "scenarios" / "frozen" / "normal-001.json").read_text(encoding="utf-8")
            )
            run_dir = root / "v-test"
            run_dir.mkdir()
            write_jsonl(run_dir / "normal-001.jsonl", scripted_events("v-test", scenario, "abc123", 1000))
            (root / "summary.json").write_text(
                '{"runner": "scripted", "release_eligible": true, "release_eligible": false}', encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "duplicate JSON object key: 'release_eligible'"):
                collect(run_dir)

            audit = root / "audit.json"
            audit.write_text(
                '{"runs": {"a": {"oracle_status": "complete", "oracle_status": "incomplete"}}}', encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "duplicate JSON object key: 'oracle_status'"):
                audit_false_completions({"a": []}, audit)

            metrics = root / "metrics.json"
            metrics.write_text('{"vtcr": 0.9, "vtcr": 0.1}', encoding="utf-8")
            with self.assertRaisesRegex(JsonInputError, "duplicate JSON object key: 'vtcr'"):
                load_metrics(metrics)

    def test_duplicate_json_keys_are_rejected_at_any_nesting_level(self) -> None:
        with self.assertRaisesRegex(JsonInputError, "duplicate JSON object key: 'timeout_s'"):
            loads('{"scenario": {"timeout_s": 1, "timeout_s": 120}}', "manifest.json")
        with self.assertRaisesRegex(JsonInputError, "duplicate JSON object key: 'ok'"):
            loads('[{"ok": 1, "ok": 2}]', "golden-set.json")

    def test_strict_json_errors_identify_source_and_never_echo_the_body(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "events.jsonl"
            path.write_text('{"run_id": "a"}\n{"secret": "hunter2", "secret": "hunter2"}\n', encoding="utf-8")
            with self.assertRaises(JsonInputError) as context:
                load_jsonl(path)
        message = str(context.exception)
        self.assertIn("events.jsonl:2", message)
        self.assertIn("'secret'", message)
        self.assertNotIn("hunter2", message)

    def test_valid_canonical_evaluation_inputs_still_load(self) -> None:
        manifest = load_scenario_manifests([ROOT / "sim" / "scenarios" / "frozen" / "normal-001.json"])
        self.assertEqual(manifest[0][1]["scenario_id"], "normal-001")
        events = scripted_events("v-test", manifest[0][1], "abc123", 1000)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "events.jsonl"
            write_jsonl(path, events)
            self.assertEqual(len(load_jsonl(path)), len(events))
            self.assertEqual(len(validate_event_log(path, "v-test--normal-001")), len(events))

    def test_statistical_report_marks_identical_versions_not_significant(self) -> None:
        metrics = [
            {"run_count": 30, "vtcr": 0.8},
            {"run_count": 30, "vtcr": 0.8},
            {"run_count": 30, "vtcr": 0.8},
        ]
        report = compare(metrics, ["A", "B", "C"])
        self.assertTrue(all(not item["statistically_significant"] for item in report["pairwise"]))
        lower, upper = wilson_interval(24, 30)
        self.assertLess(lower, 0.8)
        self.assertGreater(upper, 0.8)

    def test_release_report_requires_all_five_task_families(self) -> None:
        metrics = {
            "release_eligible": True,
            "false_completion_count": 0,
            "collision_count": 0,
            "policy_violation_count": 0,
            "vtcr": 0.8,
            "task_duration_p95_s": 119,
            "evidence_coverage": 1.0,
            "recovery_rate": 0.7,
            "state_hash_consistency": 1.0,
            "replay_success_rate": 0.95,
            "task_family_count": 4,
            "complex_task_rate": 0.5,
            "mean_observed_entities": 2.0,
            "goal_condition_coverage": 1.0,
        }
        self.assertIn("评测任务族少于 5 类", release_reasons(metrics))
        metrics["task_family_count"] = 5
        self.assertEqual(release_reasons(metrics), [])

        published_thresholds = {
            "recovery_rate": (0.0, "恢复率缺失或低于 70%"),
            "state_hash_consistency": (0.0, "state hash 一致性不是 100%"),
            "replay_success_rate": (0.0, "回放成功率缺失或低于 95%"),
            "mean_observed_entities": (0.0, "平均观测实体数缺失或低于 2"),
        }
        for metric, (failed_value, expected_reason) in published_thresholds.items():
            with self.subTest(metric=metric):
                candidate = {**metrics, metric: failed_value}
                self.assertIn(expected_reason, release_reasons(candidate))
                candidate.pop(metric)
                self.assertIn(expected_reason, release_reasons(candidate))

        all_numeric_thresholds = {
            "vtcr": "VTCR 低于 80%",
            "task_duration_p95_s": "任务 P95 缺失或未低于 120 秒",
            "evidence_coverage": "验证证据覆盖率不是 100%",
            "recovery_rate": "恢复率缺失或低于 70%",
            "state_hash_consistency": "state hash 一致性不是 100%",
            "replay_success_rate": "回放成功率缺失或低于 95%",
            "task_family_count": "评测任务族少于 5 类",
            "complex_task_rate": "复杂任务占比低于 50%",
            "mean_observed_entities": "平均观测实体数缺失或低于 2",
            "goal_condition_coverage": "目标条件覆盖率不是 100%",
        }
        for metric, expected_reason in all_numeric_thresholds.items():
            for invalid_value in (float("nan"), float("inf"), "not-a-number", True):
                with self.subTest(metric=metric, invalid_value=invalid_value):
                    candidate = {**metrics, metric: invalid_value}
                    self.assertIn(expected_reason, release_reasons(candidate))

    def _scenario(self) -> dict:
        return json.loads((ROOT / "sim" / "scenarios" / "frozen" / "normal-001.json").read_text(encoding="utf-8"))

    def test_metrics_reject_duplicate_run_ids_and_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events = scripted_events("v-test", self._scenario(), "abc123", 1000)
            for name in ("first.jsonl", "second.jsonl"):
                write_jsonl(root / name, events)
            with self.assertRaisesRegex(RuntimeError, "duplicate run_id"):
                load_runs(root)

            duplicate_keys = root / "nested"
            duplicate_keys.mkdir()
            (duplicate_keys / "keys.jsonl").write_text(
                '{"run_id": "run-1", "run_id": "run-2", "sequence_no": 0}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "duplicate JSON object key"):
                load_runs(duplicate_keys)

    def test_metrics_reject_duplicate_run_ids_with_conflicting_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "v-test"
            run_dir.mkdir()
            events = scripted_events("v-test", self._scenario(), "abc123", 1000)
            write_jsonl(run_dir / "normal-001.jsonl", events)
            drifted = [dict(event) for event in events]
            for event in drifted:
                event["evaluation"] = {**event["evaluation"], "commit": "different-commit"}
            write_jsonl(root / "drifted.jsonl", drifted)
            with self.assertRaisesRegex(RuntimeError, "conflicting commit"):
                load_runs(root)

    def test_metrics_fail_closed_on_inconsistent_run_logs(self) -> None:
        def drift_one_action(events: list[dict]) -> None:
            events[-1]["run_id"] = "other-run"

        cases = {
            "missing event_id": lambda events: [event.pop("event_id") for event in events],
            "duplicate event_id": lambda events: [event.update({"event_id": "shared"}) for event in events],
            "unknown event_type": lambda events: [event.update({"event_type": "not_a_real_event"}) for event in events],
            "non-contiguous sequence_no": lambda events: events[1].update({"sequence_no": 99}),
            "run_id drift": drift_one_action,
        }
        for expected, mutate in cases.items():
            with self.subTest(case=expected), tempfile.TemporaryDirectory() as temp_dir:
                events = scripted_events("v-test", self._scenario(), "abc123", 1000)
                mutate(events)
                path = Path(temp_dir) / "normal-001.jsonl"
                write_jsonl(path, events)
                with self.assertRaisesRegex(RuntimeError, expected):
                    load_runs(Path(temp_dir))

    def test_metrics_fail_closed_on_absent_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(RuntimeError, "does not exist"):
                load_runs(Path(temp_dir) / "missing")

            empty = Path(temp_dir) / "empty"
            empty.mkdir()
            with self.assertRaisesRegex(RuntimeError, "no JSON Lines event logs"):
                collect(empty)

    def test_metrics_preserve_version_runner_and_scenario_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "v-test"
            run_dir.mkdir()
            write_jsonl(run_dir / "normal-001.jsonl", scripted_events("v-test", self._scenario(), "abc123", 1000))
            metadata = run_metadata(load_runs(run_dir))
        self.assertEqual(
            metadata["v-test--normal-001"],
            {
                "commit": "abc123",
                "scenario_id": "normal-001",
                "seed": 1000 + self._scenario()["seed"],
                "runner": "scripted",
                "task_id": metadata["v-test--normal-001"]["task_id"],
                "scene_variant": metadata["v-test--normal-001"]["scene_variant"],
            },
        )

    def test_metrics_ingest_nested_version_directories(self) -> None:
        manifest = json.loads((ROOT / "sim" / "scenarios" / "frozen" / "normal-001.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            version_dir = root / "v-nested"
            version_dir.mkdir()
            write_jsonl(version_dir / "normal-001.jsonl", scripted_events("v-nested", manifest, "abc123", 1000))
            (root / "summary.json").write_text(
                json.dumps({"runner": "scripted", "release_eligible": False}),
                encoding="utf-8",
            )
            nested = collect(root)
            flat = collect(version_dir)
        self.assertEqual(nested["run_count"], 1)
        self.assertEqual(nested["run_count"], flat["run_count"])
        self.assertEqual(nested["vtcr"], flat["vtcr"])

    def test_runner_monotonic_elapsed_is_preferred_over_cross_node_wall_clock(self) -> None:
        wall = {"run": [{"occurred_at": "2026-01-01T00:00:00Z"}, {"occurred_at": "2026-01-01T00:10:00Z"}]}
        self.assertEqual(durations(wall), [600.0])
        self.assertEqual(duration_sources(wall), {"run": "occurred_at_wall_clock"})

        measured = {
            "run": [
                {"occurred_at": "2026-01-01T00:10:00Z", "evaluation": {"elapsed_s": 12.5}},
                {"occurred_at": "2026-01-01T00:00:00Z", "evaluation": {"elapsed_s": 0.0}},
            ]
        }
        # Wall clocks moved backwards, but the runner measured the real elapsed time.
        self.assertEqual(durations(measured), [0.0])
        self.assertEqual(duration_sources(measured), {"run": "runner_monotonic_elapsed"})

    def test_invalid_runner_elapsed_measurements_fail_closed(self) -> None:
        for elapsed in ("12.5", True, float("nan"), float("inf"), -1.0):
            with self.subTest(elapsed=elapsed):
                run = {
                    "run": [
                        {"occurred_at": "2026-01-01T00:00:00Z", "evaluation": {"elapsed_s": elapsed}},
                        {"occurred_at": "2026-01-01T00:00:10Z"},
                    ]
                }
                with self.assertRaisesRegex(RuntimeError, "invalid task duration|non-numeric elapsed_s"):
                    durations(run)

    def test_release_gate_rejects_negative_and_non_finite_durations(self) -> None:
        metrics = {
            "release_eligible": True,
            "false_completion_count": 0,
            "collision_count": 0,
            "policy_violation_count": 0,
            "vtcr": 0.9,
            "evidence_coverage": 1.0,
            "recovery_rate": 0.8,
            "state_hash_consistency": 1.0,
            "replay_success_rate": 1.0,
            "task_family_count": 5,
            "complex_task_rate": 0.6,
            "mean_observed_entities": 2.0,
            "goal_condition_coverage": 1.0,
            "task_duration_p95_s": 100.0,
            "task_duration_p50_s": 80.0,
        }
        self.assertEqual(release_reasons(metrics), [])

        negative = {**metrics, "task_duration_p95_s": -600.0}
        self.assertIn("任务时间 P95 为负数,时间证据不可信", release_reasons(negative))

        non_finite = {**metrics, "task_duration_p50_s": float("nan")}
        self.assertTrue(any("P50" in reason for reason in release_reasons(non_finite)))

        # A reversed run cannot reach the release gates as a passing duration.
        reversed_run = {
            "run": [
                {"occurred_at": "2026-08-21T00:10:00Z"},
                {"occurred_at": "2026-08-21T00:00:00Z"},
            ]
        }
        with self.assertRaisesRegex(RuntimeError, "invalid task duration"):
            durations(reversed_run)

    def test_naive_and_malformed_timestamps_fail_with_file_line_and_event_id(self) -> None:
        events = scripted_events("v-test", self._scenario(), "abc123", 1000)
        for label, value, expected in (
            ("naive", "2026-01-01T00:00:00", "timezone-naive occurred_at"),
            ("malformed", "not-a-timestamp", "malformed occurred_at"),
            ("empty", "", "missing occurred_at"),
        ):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temp_dir:
                broken = [dict(event) for event in events]
                broken[0]["occurred_at"] = value
                path = Path(temp_dir) / "events.jsonl"
                write_jsonl(path, broken)
                with self.assertRaisesRegex(RuntimeError, expected) as context:
                    validate_event_log(path, "v-test--normal-001")
                message = str(context.exception)
                self.assertIn("events.jsonl:1", message)
                self.assertIn("v-test--normal-001-evt-000", message)

    def test_sequence_order_stays_authoritative_when_wall_clocks_move_backwards(self) -> None:
        events = scripted_events("v-test", self._scenario(), "abc123", 1000)
        # Diagnostic wall timestamps from a second clock domain move backwards
        # mid-run; replay order must still follow sequence_no.
        skewed = [dict(event) for event in events]
        for index, event in enumerate(skewed):
            event["occurred_at"] = f"2026-01-01T00:00:{59 - index:02d}Z"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "events.jsonl"
            write_jsonl(path, skewed)
            loaded = validate_event_log(path, "v-test--normal-001")
        self.assertEqual([event["sequence_no"] for event in loaded], list(range(len(loaded))))

    def test_task_durations_fail_closed_on_negative_or_unusable_timestamps(self) -> None:
        negative = {"run": [{"occurred_at": "2026-01-01T00:00:10Z"}, {"occurred_at": "2026-01-01T00:00:00Z"}]}
        with self.assertRaisesRegex(RuntimeError, "invalid task duration"):
            durations(negative)

        unusable = {"run": [{"occurred_at": "not-a-timestamp"}, {"occurred_at": "2026-01-01T00:00:00Z"}]}
        with self.assertRaisesRegex(RuntimeError, "unusable occurred_at timestamps"):
            durations(unusable)

        self.assertEqual(
            durations({"run": [{"occurred_at": "2026-01-01T00:00:00Z"}, {"occurred_at": "2026-01-01T00:00:10Z"}]}),
            [10.0],
        )


class ExternalRunnerBoundTests(unittest.TestCase):
    """A declared scenario budget must bound the runner, not a global constant."""

    def _manifest(self, timeout_s: int) -> Path:
        directory = Path(tempfile.mkdtemp())
        path = directory / "scenario.json"
        path.write_text(
            json.dumps(
                {
                    "scenario_id": "probe-67",
                    "seed": 5,
                    "task_id": "task-place-red-block",
                    "world_version": "WorkbenchSim-v0",
                    "fault_type": "none",
                    "timeout_s": timeout_s,
                    "oracle_allowed": False,
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_budget_derives_from_manifest_plus_documented_startup_grace(self) -> None:
        self.assertEqual(external_timeout_budget({"timeout_s": 120}), 120 + EXTERNAL_STARTUP_GRACE_SECONDS)
        self.assertNotEqual(external_timeout_budget({"timeout_s": 120}), 900)

    def test_budget_rejects_missing_or_non_positive_timeouts(self) -> None:
        for value in (None, 0, -5, True, "120", 1.5):
            with self.subTest(timeout_s=value):
                with self.assertRaises(EvaluationInputError):
                    external_timeout_budget({"timeout_s": value})

    def test_timed_out_runner_is_killed_as_a_process_tree(self) -> None:
        directory = Path(tempfile.mkdtemp())
        marker = directory / "child.pid"
        runner = directory / "runner.py"
        runner.write_text(
            textwrap.dedent(
                """
                import os, subprocess, sys, time
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
                open(sys.argv[1], "w").write(str(child.pid))
                time.sleep(600)
                """
            ),
            encoding="utf-8",
        )
        manifest = self._manifest(1)

        started = time.monotonic()
        with self.assertRaises(ExternalRunnerTimeout) as caught:
            run_external(
                f"{sys.executable} {runner} {marker}",
                manifest,
                directory / "out.jsonl",
                1005,
                "v-test",
                timeout_s=1,
                scenario_id="probe-67",
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 20, "the runner must stop at its budget, not the old 900s constant")
        self.assertEqual(caught.exception.scenario_id, "probe-67")
        self.assertEqual(caught.exception.budget_s, 1)
        self.assertLess(caught.exception.elapsed_s, 20)

        child_pid = int(marker.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except (ProcessLookupError, PermissionError):
                alive = False
                break
            time.sleep(0.1)
        self.assertFalse(alive, f"child pid {child_pid} survived the timeout; the process tree was not terminated")

    def test_timeout_message_names_scenario_budget_and_command(self) -> None:
        directory = Path(tempfile.mkdtemp())
        runner = directory / "sleeper.py"
        runner.write_text("import time; time.sleep(600)", encoding="utf-8")

        with self.assertRaises(ExternalRunnerTimeout) as caught:
            run_external(
                f"{sys.executable} {runner}",
                self._manifest(1),
                directory / "out.jsonl",
                1005,
                "v-test",
                timeout_s=1,
                scenario_id="probe-67",
            )
        message = str(caught.exception)
        self.assertIn("probe-67", message)
        self.assertIn("budget 1s", message)
        self.assertIn(str(runner), message)

    def test_failed_runner_raises_without_leaving_output(self) -> None:
        directory = Path(tempfile.mkdtemp())
        output = directory / "out.jsonl"
        runner = directory / "failing.py"
        runner.write_text("import sys; sys.exit(3)", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, r"failed \(3\)"):
            run_external(
                f"{sys.executable} {runner}",
                self._manifest(5),
                output,
                1005,
                "v-test",
                timeout_s=5,
                scenario_id="probe-67",
            )
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
