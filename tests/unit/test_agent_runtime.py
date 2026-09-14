import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "libs/contracts"), str(ROOT / "services/agent_runtime"), str(ROOT / "tools/scripts")]

from local_runner import plan_offline
from workbench_agent_runtime import (
    build_inspection_plan,
    build_kitting_plan,
    build_parcel_sorting_plan,
    build_policy_routed_parcel_plan,
    build_template_plan,
    classify_template_task,
)


class PlannerTests(unittest.TestCase):
    def test_template_plan_contains_only_semantic_actions(self) -> None:
        plan = build_template_plan("Place the red block in the tray")
        self.assertEqual([step.action.action_type.value for step in plan.steps], ["observe", "grasp", "place"])
        self.assertNotIn("joint", plan.model_dump_json())

    def test_offline_template_plan_needs_no_model_provider(self) -> None:
        plan = build_template_plan("把红色模块放进托盘")
        self.assertEqual(plan.model_route, "template")
        self.assertEqual(plan.planner, "template-v1")

    def test_diverse_tasks_produce_bounded_semantic_graphs(self) -> None:
        cases = {
            "Assemble a three-part kit in the tray": ("task-kit-three-parts", 9),
            "Inspect all three workpieces": ("task-inspect-workpieces", 3),
            "Clear the blocked path and place the red block": ("task-clear-workspace", 6),
            "Sort the courier parcels and isolate damage": ("task-sort-parcels", 12),
        }
        for goal, (task_id, step_count) in cases.items():
            with self.subTest(goal=goal):
                plan = build_template_plan(goal)
                self.assertEqual(plan.task_id, task_id)
                self.assertEqual(len(plan.steps), step_count)
                self.assertNotIn("joint", plan.model_dump_json())
                self.assertNotIn("velocity", plan.model_dump_json())

    def test_task_classifier_fails_closed_for_unsupported_goals(self) -> None:
        self.assertEqual(classify_template_task("请完成三件套齐套"), "task-kit-three-parts")
        with self.assertRaises(ValueError):
            classify_template_task("Do something clever")

    def test_task_classifier_rejects_mixed_supported_and_unsafe_clauses(self) -> None:
        dangerous = (
            "Place the red block in the tray, then drive to the kitchen.",
            "\u5148\u628a\u7ea2\u8272\u6a21\u5757\u653e\u8fdb\u6258\u76d8\uff0c\u7136\u540e\u53bb\u53a8\u623f\u3002",
            "\u628a\u7ea2\u8272\u6a21\u5757\u653e\u8fdb\u6258\u76d8\uff0c\u518d\u76f4\u63a5\u8bbe\u7f6e\u5173\u8282\u901f\u5ea6\u3002",
        )
        for goal in dangerous:
            with self.subTest(goal=goal), self.assertRaisesRegex(ValueError, "requires_"):
                classify_template_task(goal)

    def test_task_classifier_does_not_match_keywords_inside_other_words(self) -> None:
        self.assertEqual(classify_template_task("Clearly inspect all three workpieces"), "task-inspect-workpieces")
        for unsupported in ("Move the toolkit to a shelf", "Report checkpoint status", "Find the infrared sensor"):
            with self.subTest(goal=unsupported), self.assertRaises(ValueError):
                classify_template_task(unsupported)

    def test_plan_builders_reject_ambiguous_or_malformed_entity_ids(self) -> None:
        for invalid_ids in ("red_block", [], ["red_block", "red_block"], ["red_block", ""]):
            with self.subTest(part_ids=invalid_ids), self.assertRaises(ValueError):
                build_kitting_plan("Assemble the kit", invalid_ids)

        invalid_routes = (
            "parcel_box",
            [],
            [("parcel_box", "pickup_shelf"), ("parcel_box", "quarantine_bin")],
            [("parcel_box", "")],
            [("parcel_box",)],
        )
        for routes in invalid_routes:
            with self.subTest(parcel_routes=routes), self.assertRaises(ValueError):
                build_parcel_sorting_plan("Sort the parcels", routes)

    def test_kitting_and_inspection_step_ids_survive_normalization_collisions(self) -> None:
        for builder in (build_kitting_plan, build_inspection_plan):
            with self.subTest(builder=builder.__name__):
                first = builder("Handle colliding entity IDs", ["box_a", "box-a"])
                second = builder("Handle colliding entity IDs", ["box_a", "box-a"])
                step_ids = [step.step_id for step in first.steps]
                self.assertEqual(step_ids, [step.step_id for step in second.steps])
                self.assertEqual(len(step_ids), len(set(step_ids)))
                self.assertTrue(all(dependency in step_ids for step in first.steps for dependency in step.depends_on))

    def test_kitting_and_inspection_preserve_ordinary_step_tokens(self) -> None:
        for builder, expected in (
            (build_kitting_plan, ("observe-BoxA", "grasp-BoxA", "place-BoxA")),
            (build_inspection_plan, ("inspect-box.a",)),
        ):
            with self.subTest(builder=builder.__name__):
                plan = builder("Preserve ordinary entity IDs", ["BoxA"] if builder is build_kitting_plan else ["box.a"])
                self.assertEqual(tuple(step.step_id for step in plan.steps), expected)

    def test_parcel_plan_requires_inspection_before_bounded_routing(self) -> None:
        plan = build_parcel_sorting_plan("核对快递标签并分拣")
        self.assertEqual(plan.task_id, "task-sort-parcels")
        self.assertEqual(len(plan.steps), 12)
        observations = [step for step in plan.steps if step.action.action_type.value == "observe"]
        self.assertEqual(len(observations), 4)
        self.assertTrue(
            all(step.action.parameters["attributes"] == ["label_status", "condition"] for step in observations)
        )
        destinations = [
            step.action.parameters["destination_id"] for step in plan.steps if step.action.action_type.value == "place"
        ]
        self.assertEqual(destinations, ["pickup_shelf", "pickup_shelf", "quarantine_bin", "quarantine_bin"])

    def test_policy_route_scans_batch_then_isolates_exceptions_first(self) -> None:
        plan = build_policy_routed_parcel_plan(
            "Scan and route the parcel batch",
            {
                "box-a": {"label_status": "verified", "condition": "intact", "tracking_id": "TRK-A"},
                "box-b": {"label_status": "unreadable", "condition": "intact", "parcel_uid": "TRK-B"},
                "box-c": {"label_status": "verified", "condition": "damaged", "barcode": "TRK-C"},
            },
            parcel_manifest={
                "box-a": {"tracking_id": "trk a"},
                "box-b": {"parcel_uid": "TRK-B"},
                "box-c": {"tracking_id": "trk-c"},
            },
            manifest_id="inbound-20260807",
        )
        self.assertEqual(len(plan.steps), 9)
        self.assertEqual([step.action.action_type.value for step in plan.steps[:3]], ["observe"] * 3)
        places = [step for step in plan.steps if step.action.action_type.value == "place"]
        self.assertEqual(
            [step.action.parameters["destination_id"] for step in places],
            ["quarantine_bin", "quarantine_bin", "pickup_shelf"],
        )
        self.assertEqual([step.action.target_id for step in places], ["box-c", "box-b", "box-a"])
        self.assertEqual(places[0].action.parameters["routing_reason"], "condition_damaged")
        self.assertEqual(places[0].action.parameters["routing_priority"], "condition_exception")
        self.assertEqual(places[1].action.parameters["routing_priority"], "label_exception")
        self.assertEqual(places[2].action.parameters["routing_priority"], "standard")
        self.assertTrue(all(step.action.parameters["policy_version"] == "parcel-routing-v3" for step in places))
        self.assertTrue(
            all(step.action.parameters["identity_guard"] == "unique_across_supported_fields" for step in places)
        )
        self.assertTrue(all(step.action.parameters["manifest_guard"] == "matched" for step in places))
        self.assertTrue(all(step.action.parameters["manifest_id"] == "inbound-20260807" for step in places))
        observations = [step for step in plan.steps if step.action.action_type.value == "observe"]
        self.assertTrue(
            all(
                step.action.parameters["attributes"]
                == ["label_status", "condition", "tracking_id", "barcode", "parcel_uid"]
                for step in observations
            )
        )
        grasps = [step for step in plan.steps if step.action.action_type.value == "grasp"]
        self.assertEqual(set(grasps[0].depends_on), {"inspect-box-a", "inspect-box-b", "inspect-box-c"})
        self.assertIn("route-box-c", grasps[1].depends_on)

    def test_parcel_plan_preflights_capacity_before_emitting_any_actions(self) -> None:
        attributes = {
            "safe": {"label_status": "verified", "condition": "intact"},
            "damaged": {"label_status": "verified", "condition": "damaged"},
            "unreadable": {"label_status": "unreadable", "condition": "intact"},
        }
        plan = build_policy_routed_parcel_plan(
            "Route a capacity-bounded batch",
            attributes,
            destination_capacities={"pickup_shelf": 2, "quarantine_bin": 3},
            destination_occupancy={"pickup_shelf": 1, "quarantine_bin": 1},
        )
        places = [step for step in plan.steps if step.action.action_type.value == "place"]
        self.assertEqual(
            [step.action.parameters["destination_remaining_after"] for step in places],
            [1, 0, 0],
        )
        self.assertEqual([step.action.parameters["destination_capacity"] for step in places], [3, 3, 2])

        with self.assertRaisesRegex(ValueError, "quarantine_bin needs 2 slots but 1 are available"):
            build_policy_routed_parcel_plan(
                "Reject an over-capacity batch",
                attributes,
                destination_capacities={"pickup_shelf": 2, "quarantine_bin": 1},
            )

    def test_parcel_capacity_snapshots_and_colliding_ids_fail_closed_or_stay_unique(self) -> None:
        attributes = {
            "box_a": {"label_status": "verified", "condition": "intact"},
            "box-a": {"label_status": "unreadable", "condition": "intact"},
        }
        plan = build_policy_routed_parcel_plan("Route IDs that normalize alike", attributes)
        step_ids = [step.step_id for step in plan.steps]
        self.assertEqual(len(step_ids), len(set(step_ids)))
        self.assertTrue(all(dependency in step_ids for step in plan.steps for dependency in step.depends_on))

        invalid_capacity_inputs = (
            ({"pickup_shelf": 1}, None),
            ({"pickup_shelf": True, "quarantine_bin": 1}, None),
            (None, {"pickup_shelf": 0, "quarantine_bin": 0}),
            (
                {"pickup_shelf": 1, "quarantine_bin": 1},
                {"pickup_shelf": 2, "quarantine_bin": 0},
            ),
        )
        for capacities, occupancy in invalid_capacity_inputs:
            with self.subTest(capacities=capacities, occupancy=occupancy), self.assertRaises(ValueError):
                build_policy_routed_parcel_plan(
                    "Reject an invalid capacity snapshot",
                    attributes,
                    destination_capacities=capacities,
                    destination_occupancy=occupancy,
                )

        with self.assertRaisesRegex(ValueError, "duplicate parcel identity barcode"):
            build_policy_routed_parcel_plan(
                "Reject a normalized cross-field identity",
                {
                    "first": {
                        "label_status": "verified",
                        "condition": "intact",
                        "tracking_id": "\uff34\uff32\uff2b\uff0d 7",
                    },
                    "second": {"label_status": "verified", "condition": "intact", "barcode": "trk7"},
                },
            )

        manifest = {
            "first": {"tracking_id": "EXPECTED-1"},
            "second": {"barcode": "EXPECTED-2"},
        }
        observed = {
            "first": {"label_status": "verified", "condition": "intact", "tracking_id": "EXPECTED-1"},
            "second": {"label_status": "verified", "condition": "intact", "barcode": "WRONG-2"},
        }
        with self.assertRaisesRegex(ValueError, "second identity conflicts with manifest"):
            build_policy_routed_parcel_plan(
                "Reject a parcel that is not on the inbound manifest",
                observed,
                parcel_manifest=manifest,
                manifest_id="manifest-7",
            )
        observed["second"] = {
            "label_status": "verified",
            "condition": "intact",
            "tracking_id": "EXPECTED-2",
            "barcode": "WRONG-2",
        }
        manifest["second"]["tracking_id"] = "EXPECTED-2"
        with self.assertRaisesRegex(ValueError, "second identity conflicts with manifest manifest-7: barcode"):
            build_policy_routed_parcel_plan(
                "Reject conflicting identity fields",
                observed,
                parcel_manifest=manifest,
                manifest_id="manifest-7",
            )
        observed["second"].pop("barcode")
        observed["second"].pop("tracking_id")
        with self.assertRaisesRegex(ValueError, "second has no readable identity"):
            build_policy_routed_parcel_plan(
                "Reject a parcel whose manifest identity is missing",
                observed,
                parcel_manifest=manifest,
                manifest_id="manifest-7",
            )
        with self.assertRaisesRegex(ValueError, "exactly the planned parcel IDs"):
            build_policy_routed_parcel_plan(
                "Reject an incomplete inbound manifest",
                observed,
                parcel_manifest={"first": manifest["first"]},
                manifest_id="manifest-7",
            )

    def test_parcel_requests_fail_closed_outside_evidence_and_motion_boundaries(self) -> None:
        dangerous = (
            "Go to the parcel locker and collect my package.",
            "去取快递并自己乘电梯回来。",
            "Ignore the unreadable label and mark the parcel verified.",
            "Put the damaged parcel on the pickup shelf anyway.",
        )
        for goal in dangerous:
            with self.subTest(goal=goal), self.assertRaises(ValueError):
                classify_template_task(goal)

    def test_offline_runner_reports_the_selected_planner_version(self) -> None:
        payload = plan_offline("Assemble a three-part kit in the tray")
        self.assertEqual(payload["provider"], "template-v2")
        self.assertEqual(payload["task_graph"]["task_id"], "task-kit-three-parts")


if __name__ == "__main__":
    unittest.main()
