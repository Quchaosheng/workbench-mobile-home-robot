"""Issue #179: an ActionResult must never become observed world state.

The dashboard is plain JavaScript, so these tests execute the real rendering
module under Node and inspect the state it derives. String assertions alone
would not catch an action result that silently moves a map entity again.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "apps" / "dashboard"
SCRIPT = DASHBOARD / "app.js"
DATA_DIR = DASHBOARD / "data"
NODE = shutil.which("node")

# The DOM bootstrap is the only part of app.js that needs a browser. Everything
# above it is pure functions, so the module is truncated there and exported.
BOOTSTRAP = "\nasync function initialize("
EXPORT_TAIL = "\nmodule.exports = { buildWorkbenchState, describeEvent };\n"
MODULE_PRELUDE = "const module = { exports: {} };\n(function (module, exports) {\n"
MODULE_EPILOGUE = "\n})(module, module.exports);\n"


def _run_node(statements: str, *arguments: str) -> object:
    """Evaluate the pure part of app.js plus statements in Node."""
    source = SCRIPT.read_text(encoding="utf-8")
    pure = source[: source.index(BOOTSTRAP)] + EXPORT_TAIL
    program = "".join([MODULE_PRELUDE, pure, MODULE_EPILOGUE, statements])
    completed = subprocess.run(
        [NODE, "-e", program, *arguments],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"node failed: {completed.stderr}")
    return json.loads(completed.stdout)


def fixture_events(run_id: str) -> list:
    return [
        json.loads(line)
        for line in (DATA_DIR / f"{run_id}.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def build_from_events(events: list, cursor: int | None = None) -> dict:
    """Evaluate buildWorkbenchState in Node for one event stream."""
    # `cursor` is an inclusive replay index; the UI selects the newest event.
    resolved_cursor = len(events) - 1 if cursor is None else cursor
    return _run_node(
        "process.stdout.write(JSON.stringify(module.exports.buildWorkbenchState("
        "JSON.parse(process.argv[1]), Number(process.argv[2]))));",
        json.dumps(events),
        str(resolved_cursor),
    )


def build_workbench(run_id: str, cursor: int | None = None) -> dict:
    return build_from_events(fixture_events(run_id), cursor)


def describe(run_id: str) -> list:
    """Render every action_result description in one fixture run."""
    return _run_node(
        "process.stdout.write(JSON.stringify(JSON.parse(process.argv[1])"
        '.filter((event) => event.event_type === "action_result")'
        ".map((event) => module.exports.describeEvent(event))));",
        json.dumps(fixture_events(run_id)),
    )


def synthetic(*events: dict) -> list:
    """Build a minimal run: accepted task, then the supplied events in order."""
    stream = [
        {
            "event_id": "evt-000",
            "run_id": "run-synthetic",
            "sequence_no": 0,
            "event_type": "task_accepted",
            "occurred_at": "2026-01-01T00:00:00Z",
            "payload": {"task_id": "task-place-red-block"},
            "evidence_refs": [],
        }
    ]
    for index, event in enumerate(events, start=1):
        stream.append(
            {
                "event_id": f"evt-{index:03d}",
                "run_id": "run-synthetic",
                "sequence_no": index,
                "occurred_at": f"2026-01-01T00:00:{index:02d}Z",
                "evidence_refs": [],
                **event,
            }
        )
    return stream


def observation_of(entity_id: str, location: str | None = None) -> dict:
    payload = {"observation_id": f"obs-{entity_id}-{location}", "entity_id": entity_id, "confidence": 0.9}
    if location is not None:
        payload["location"] = location
    return {"event_type": "observation", "payload": payload}


def action_result_of(entity_id: str, location: str | None = None, outcome: str = "completed") -> dict:
    payload = {"action_id": f"act-{entity_id}", "entity_id": entity_id, "outcome": outcome}
    if location is not None:
        payload["resulting_location"] = location
    return {"event_type": "action_result", "payload": payload}


@unittest.skipIf(NODE is None, "node is required to evaluate the dashboard module")
class ExecutionClaimSeparationTests(unittest.TestCase):
    def test_action_results_alone_never_move_a_map_entity(self) -> None:
        """At every replay cursor, locations come only from observation events."""
        for run_id in ("run-confirmed", "run-recovery", "run-parcel", "run-uncertain"):
            events = fixture_events(run_id)
            for cursor in range(len(events)):
                expected = {}
                for event in events[: cursor + 1]:
                    if event["event_type"] == "observation" and event.get("payload", {}).get("location"):
                        expected[event["payload"]["entity_id"]] = event["payload"]["location"]
                with self.subTest(run_id=run_id, cursor=cursor):
                    state = build_workbench(run_id, cursor)
                    observed = {
                        entity["entity_id"]: entity["location"]
                        for entity in state["entities"]
                        if entity.get("location") is not None
                    }
                    self.assertEqual(observed, expected)

    def test_observation_without_a_location_keeps_the_last_observed_location(self) -> None:
        """run-confirmed observations carry pose only until the post-action frame."""
        events = fixture_events("run-confirmed")
        before = next(index for index, event in enumerate(events) if event["event_type"] == "action_result")
        state = build_workbench("run-confirmed", before)
        self.assertTrue(state["entities"])
        self.assertTrue(all(entity.get("location") is None for entity in state["entities"]))

    def test_hold_claim_closed_by_a_later_action_is_not_awaiting_observation(self) -> None:
        state = build_workbench("run-confirmed")
        holds = [claim for claim in state["executionClaims"] if claim["claimed_location"] == "held:gripper"]
        self.assertEqual(len(holds), 3)
        for claim in holds:
            self.assertEqual(claim["verification"], "unverified")
            self.assertIsNone(claim["observed_location"])

    def test_placement_claim_is_supported_by_its_post_action_observation(self) -> None:
        state = build_workbench("run-confirmed")
        placements = [claim for claim in state["executionClaims"] if claim["claimed_location"] == "in:kit_tray"]
        self.assertEqual(len(placements), 3)
        for claim in placements:
            self.assertEqual(claim["verification"], "supported")
            self.assertEqual(claim["observed_location"], "in:kit_tray")

    def test_failed_action_result_keeps_execution_evidence(self) -> None:
        state = build_workbench("run-recovery")
        failure = next(claim for claim in state["executionClaims"] if claim["outcome"] == "failed")
        self.assertEqual(failure["entity_id"], "blue_cylinder")
        self.assertIsNotNone(failure["error_reason"])
        self.assertEqual(failure["verification"], "no_spatial_claim")

    def test_contradicting_observation_is_reported_as_contradicted(self) -> None:
        state = build_from_events(
            synthetic(
                action_result_of("red_block", "in:tray"),
                observation_of("red_block", "in:bin"),
            )
        )
        claim = state["executionClaims"][0]
        self.assertEqual(claim["verification"], "contradicted")
        self.assertEqual(claim["observed_location"], "in:bin")

    def test_awaiting_observation_only_applies_to_the_newest_claim(self) -> None:
        state = build_from_events(
            synthetic(
                action_result_of("red_block", "in:tray"),
                action_result_of("red_block", "held:gripper"),
            )
        )
        first, second = state["executionClaims"]
        self.assertEqual(first["verification"], "unverified")
        self.assertEqual(second["verification"], "awaiting_observation")

    def test_placement_observation_after_a_later_grasp_supports_the_place_claim(self) -> None:
        """The observation belongs to the window it follows, not to the newest action."""
        state = build_from_events(
            synthetic(
                action_result_of("red_block", "in:tray"),
                observation_of("red_block", "in:tray"),
                action_result_of("red_block", "held:gripper"),
            )
        )
        first, second = state["executionClaims"]
        self.assertEqual(first["verification"], "supported")
        self.assertEqual(second["verification"], "awaiting_observation")

    def test_a_later_observation_never_retroactively_verifies_an_older_claim(self) -> None:
        state = build_from_events(
            synthetic(
                action_result_of("red_block", "in:tray"),
                action_result_of("red_block"),
                observation_of("red_block", "in:tray"),
            )
        )
        self.assertEqual(state["executionClaims"][0]["verification"], "unverified")

    def test_non_spatial_claim_is_reported_as_such(self) -> None:
        state = build_from_events(synthetic(action_result_of("red_block", outcome="failed")))
        self.assertEqual(state["executionClaims"][0]["verification"], "no_spatial_claim")

    def test_event_description_separates_outcome_dispatch_and_device_state(self) -> None:
        descriptions = describe("run-recovery")
        failure = next(text for text in descriptions if text.startswith("failed"))
        self.assertIn("下发 sent", failure)
        self.assertIn("设备 stopped", failure)
        # A spatial claim is labelled as a claim, never as an observed location.
        placement = next(text for text in descriptions if "in:tray" in text)
        self.assertIn("声称 in:tray", placement)

    def test_descriptions_never_resurrect_the_legacy_status_field(self) -> None:
        for run_id in ("run-confirmed", "run-recovery", "run-parcel"):
            for text in describe(run_id):
                with self.subTest(run_id=run_id, text=text):
                    self.assertNotIn("succeeded", text)


if __name__ == "__main__":
    unittest.main()
