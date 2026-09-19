"""Issue #303: multi-scenario run identity, bounded filters and evidence timelines.

The read-model assertions are Python because the projection is Python. The
Dashboard assertions execute the real `app.js` under Node, because a filter UI
that builds its options from a hardcoded list, or an unknown evidence state
rendered as a success, would satisfy any string check.

Two properties are pinned deliberately and are the reason this file exists:

* The **outcome** axis (the verifier's verdict) and the **evidence** axis (the
  chain that led to it) stay distinct. `run-recovery` ends `confirmed` while its
  evidence chain still contains the refutation it recovered from, so a projection
  that collapsed the two would report either a failed run that succeeded or a
  successful run with no visible recovery.
* A filter value outside the run set's facet list is **refused**, never silently
  ignored. A silently ignored filter returns the whole set and is
  indistinguishable from a filter that matched everything.
"""

import json
import shutil
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "backend"))

from workbench_backend.read_model import (
    EVIDENCE_STATES,
    MAX_RUN_FACET_VALUES,
    MAX_RUN_PAGE_SIZE,
    OUTCOME_FILTERS,
    RUN_FILTER_KEYS,
    TIMELINE_PHASES,
    DashboardReadModel,
    RunFilterError,
    filter_runs,
    run_evidence_state,
    run_facets,
    run_timeline,
    scenario_identity_for,
    timeline_phase,
)
from workbench_backend.server import create_server

DASHBOARD = ROOT / "apps" / "dashboard"
SCRIPT = DASHBOARD / "app.js"
NODE = shutil.which("node")

# The DOM bootstrap is the only part of app.js that needs a browser, so the
# module is truncated there and its pure helpers are exported for Node.
BOOTSTRAP = "\nasync function initialize("
EXPORT_TAIL = (
    "\nmodule.exports = { filterOptions, activeSelection, runQuery, filterRunList, "
    "evidenceReading, outcomeReading, scenarioReading, timelineView, pageSummary };\n"
)
MODULE_PRELUDE = "const module = { exports: {} };\n(function (module, exports) {\n"
MODULE_EPILOGUE = "\n})(module, module.exports);\n"


def _run_node(expression: str, *arguments: object) -> object:
    """Evaluate one Dashboard helper in Node with JSON arguments."""
    source = SCRIPT.read_text(encoding="utf-8")
    pure = source[: source.index(BOOTSTRAP)] + EXPORT_TAIL
    program = "".join(
        [
            MODULE_PRELUDE,
            pure,
            MODULE_EPILOGUE,
            f"process.stdout.write(JSON.stringify({expression}));",
        ]
    )
    completed = subprocess.run(
        [NODE, "-e", program, *[json.dumps(argument) for argument in arguments]],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"node failed: {completed.stderr}")
    return json.loads(completed.stdout)


def events_with(types: list[tuple[str, dict]]) -> list[dict]:
    """Build a minimal valid run from (event_type, payload) pairs."""
    return [
        {
            "event_id": f"event-{index}",
            "run_id": "synthetic",
            "sequence_no": index,
            "event_type": event_type,
            "occurred_at": "2026-08-06T00:00:00Z",
            "payload": payload,
        }
        for index, (event_type, payload) in enumerate(types)
    ]


class ScenarioIdentityTests(unittest.TestCase):
    def test_registry_task_ids_resolve_to_their_committed_identity(self) -> None:
        cases = {
            "task-place-red-block": ("pick-place-red-block", "1.0", "pick-place-red-block@1.0"),
            "task-kit-three-parts": ("kit-three-parts", "0.2", "kit-three-parts@0.2"),
            "task-clear-workspace": ("clear-workspace", "0.2", "clear-workspace@0.2"),
            "task-inspect-workpieces": ("inspect-workpieces", "0.2", "inspect-workpieces@0.2"),
            "task-sort-parcels": ("sort-parcels", "0.2", "sort-parcels@0.2"),
        }
        for task_id, (scenario_id, version, label) in cases.items():
            with self.subTest(task_id=task_id):
                resolved = scenario_identity_for(task_id)
                self.assertEqual(resolved["scenario_id"], scenario_id)
                self.assertEqual(resolved["scenario_version"], version)
                self.assertEqual(resolved["scenario_label"], label)
                self.assertEqual(resolved["scenario_source"], "registry")

    def test_an_unregistered_task_id_reports_unresolved_rather_than_a_guess(self) -> None:
        for task_id in ("window-scan-001", "", None, 17, ["task-kit-three-parts"]):
            with self.subTest(task_id=task_id):
                resolved = scenario_identity_for(task_id)
                self.assertIsNone(resolved["scenario_id"])
                self.assertIsNone(resolved["scenario_version"])
                self.assertIsNone(resolved["scenario_label"])
                self.assertEqual(resolved["scenario_source"], "unresolved")


class EvidenceStateTests(unittest.TestCase):
    def test_a_recovered_run_stays_refuted_on_the_evidence_axis(self) -> None:
        events = events_with(
            [
                ("task_accepted", {"task_id": "task-clear-workspace"}),
                ("verification", {"status": "refuted"}),
                ("recovery_started", {"reason": "stale_observation"}),
                ("recovery_complete", {"attempt": 2}),
                ("verification", {"status": "confirmed"}),
                ("task_terminal", {"status": "confirmed"}),
            ]
        )
        # No verifier ran yet: the run is live, not failed and not unexecuted.
        self.assertEqual(run_evidence_state(events[:1], None), "running")
        # A refutation on a live run is refuted; the same refutation on a run that
        # then ended without a second verifier is failed.
        self.assertEqual(run_evidence_state(events[:2], events[1]), "refuted")
        self.assertEqual(run_evidence_state([*events[:2], events[-1]], events[1]), "failed")
        self.assertEqual(run_evidence_state(events, events[4]), "refuted")

    def test_a_run_that_ended_without_a_verifier_is_not_executed(self) -> None:
        events = events_with(
            [
                ("task_accepted", {"task_id": "task-kit-three-parts"}),
                ("task_terminal", {"status": "confirmed"}),
            ]
        )
        self.assertEqual(run_evidence_state(events, None), "not_executed")
        self.assertEqual(run_evidence_state(events[:1], None), "running")

    def test_insufficient_evidence_is_its_own_state(self) -> None:
        verification = {"event_type": "verification", "payload": {"status": "insufficient_evidence"}}
        events = events_with(
            [
                ("task_accepted", {"task_id": "task-inspect-workpieces"}),
                ("verification", {"status": "insufficient_evidence"}),
                ("task_terminal", {"status": "insufficient_evidence"}),
            ]
        )
        self.assertEqual(run_evidence_state(events, verification), "insufficient_evidence")

    def test_the_evidence_vocabulary_matches_the_committed_product_template(self) -> None:
        # The template and the feedback record freeze the words; the projection may
        # not invent a synonym because a new word is a new product decision.
        for name in (
            "docs/product/design-partner-scenario-template.md",
            "docs/product/feedback-record-template.md",
        ):
            text = (ROOT / name).read_text(encoding="utf-8")
            for word in ("confirmed", "refuted", "insufficient_evidence", "failed", "not_executed"):
                with self.subTest(document=name, word=word):
                    self.assertIn(word, text)
        self.assertEqual(
            EVIDENCE_STATES,
            {"confirmed", "refuted", "insufficient_evidence", "failed", "not_executed", "running"},
        )


class TimelineTests(unittest.TestCase):
    def test_every_event_type_maps_to_exactly_one_phase(self) -> None:
        cases = {
            "action_request": "execution",
            "action_result": "execution",
            "observation": "observation",
            "verification": "verification",
            "recovery_started": "recovery",
            "recovery_complete": "recovery",
            "task_accepted": "context",
            "task_graph": "context",
            "task_terminal": "context",
            "fault": "context",
            "tool_call": "context",
            "an_unknown_future_type": "context",
        }
        for event_type, phase in cases.items():
            with self.subTest(event_type=event_type):
                self.assertEqual(timeline_phase(event_type), phase)
                self.assertIn(phase, TIMELINE_PHASES)

    def test_the_timeline_keeps_sequence_order_and_the_recovery_boundary(self) -> None:
        events = events_with(
            [
                ("task_accepted", {"task_id": "task-clear-workspace"}),
                ("observation", {"entity_id": "blue_cylinder"}),
                ("verification", {"status": "refuted"}),
                ("recovery_started", {"reason": "stale_observation"}),
                ("verification", {"status": "confirmed"}),
                ("task_terminal", {"status": "confirmed"}),
            ]
        )
        items = run_timeline(events)
        self.assertEqual([item["sequence_no"] for item in items], list(range(len(events))))
        self.assertEqual(
            [item["phase"] for item in items],
            ["context", "observation", "verification", "recovery", "verification", "context"],
        )
        # The first attempt stays visible as a distinct refuted item instead of
        # being overwritten by the recovery that superseded it.
        self.assertEqual(items[2]["payload"]["status"], "refuted")
        self.assertNotEqual(items[2]["event_id"], items[4]["event_id"])

    def test_a_recovery_bearing_stream_is_accepted_by_the_read_model(self) -> None:
        # Before this change the read model refused recovery_started/recovery_complete
        # as unknown event types, so a real recovery run could not be displayed.
        model = DashboardReadModel(DASHBOARD / "data")
        summary = model.summarize(
            events_with(
                [
                    ("task_accepted", {"task_id": "task-clear-workspace"}),
                    ("recovery_started", {"reason": "stale_observation"}),
                    ("recovery_complete", {"attempt": 2}),
                    ("verification", {"status": "confirmed"}),
                    ("task_terminal", {"status": "confirmed"}),
                ]
            )
        )
        self.assertEqual(summary["evidence"], "confirmed")
        self.assertEqual(summary["outcome"], "confirmed")


class BoundedFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.model = DashboardReadModel(DASHBOARD / "data")
        cls.runs = cls.model.list_runs()

    def test_the_bounded_facets_describe_only_values_the_run_set_contains(self) -> None:
        facets = run_facets(self.runs)
        self.assertEqual(sorted(facets), sorted(RUN_FILTER_KEYS))
        self.assertEqual(
            facets["scenario_id"],
            ["clear-workspace", "inspect-workpieces", "kit-three-parts", "sort-parcels"],
        )
        self.assertEqual(facets["scenario_version"], ["0.2"])
        self.assertEqual(facets["outcome"], ["confirmed", "insufficient_evidence"])
        self.assertEqual(facets["evidence"], ["confirmed", "insufficient_evidence", "refuted"])
        for values in facets.values():
            self.assertLessEqual(len(values), MAX_RUN_FACET_VALUES)
            self.assertEqual(values, sorted(values))

    def test_filtering_is_deterministic_and_preserves_the_committed_order(self) -> None:
        first = filter_runs(self.runs, {"evidence": "refuted"})
        second = filter_runs(self.runs, {"evidence": "refuted"})
        self.assertEqual([run["run_id"] for run in first], [run["run_id"] for run in second])
        # run-recovery sorts after the parcel run, and both keep that relative order.
        self.assertEqual(
            [run["run_id"] for run in first],
            ["dashboard-parcel--parcel-intake-003", "run-recovery"],
        )
        self.assertEqual(
            [run["run_id"] for run in filter_runs(self.runs, {"scenario_id": "sort-parcels"})],
            ["dashboard-parcel--parcel-intake-003"],
        )
        self.assertEqual(
            [run["run_id"] for run in filter_runs(self.runs, {"evidence": "refuted", "outcome": "confirmed"})],
            ["dashboard-parcel--parcel-intake-003", "run-recovery"],
        )

    def test_an_unknown_filter_value_is_refused_rather_than_ignored(self) -> None:
        for key, value in (
            ("scenario_id", "no-such-scenario"),
            ("scenario_version", "9.9"),
            ("outcome", "not-a-verdict"),
            ("evidence", "succeeded"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(RunFilterError) as caught:
                filter_runs(self.runs, {key: value})
            self.assertEqual(caught.exception.key, key)
        self.assertEqual(OUTCOME_FILTERS, {"confirmed", "refuted", "insufficient_evidence", "none", "running"})

    def test_pagination_reports_the_total_it_is_a_page_of(self) -> None:
        page = self.model.query_runs(page=2, page_size=1)
        self.assertEqual(len(page["runs"]), 1)
        self.assertEqual(page["total"], 4)
        self.assertEqual(page["unfiltered_count"], 4)
        self.assertEqual((page["page"], page["page_size"]), (2, 1))

        filtered = self.model.query_runs({"evidence": "refuted"}, page=1, page_size=1)
        self.assertEqual(filtered["total"], 2)
        self.assertEqual(filtered["unfiltered_count"], 4)

        for bad in ({"page": 0}, {"page_size": 0}, {"page_size": MAX_RUN_PAGE_SIZE + 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.model.query_runs(**bad)


class BackendRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server("127.0.0.1", 0, data_dir=DASHBOARD / "data")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def read(self, path: str, method: str = "GET") -> tuple[int, dict]:
        request = urllib.request.Request(f"{self.base_url}{path}", method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_the_unbounded_envelope_is_preserved_for_the_unpaged_request(self) -> None:
        # The split-host controller reads this exact shape, so adding the bounded
        # projection must not change it. A filter or a page opts into the richer
        # envelope instead.
        status, payload = self.read("/api/v1/runs")
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"runs", "read_only"})
        status, bounded = self.read("/api/v1/runs?page_size=200")
        self.assertEqual(status, 200)
        self.assertTrue({"total", "unfiltered_count", "page", "page_size", "max_page_size", "facets"} <= set(bounded))

    def test_run_list_carries_scenario_identity_outcome_and_evidence(self) -> None:
        status, payload = self.read("/api/v1/runs?page_size=200")
        self.assertEqual(status, 200)
        runs = {run["run_id"]: run for run in payload["runs"]}
        self.assertEqual(runs["run-confirmed"]["scenario_label"], "kit-three-parts@0.2")
        self.assertEqual(runs["run-confirmed"]["scenario_source"], "registry")
        self.assertEqual(runs["dashboard-parcel--parcel-intake-003"]["scenario_id"], "sort-parcels")
        self.assertEqual(runs["run-uncertain"]["scenario_id"], "inspect-workpieces")
        self.assertEqual(runs["run-recovery"]["scenario_id"], "clear-workspace")
        # Outcome and evidence answer different questions and are both present.
        self.assertEqual(runs["run-recovery"]["outcome"], "confirmed")
        self.assertEqual(runs["run-recovery"]["evidence"], "refuted")
        self.assertEqual(runs["run-uncertain"]["outcome"], "insufficient_evidence")
        self.assertEqual(runs["run-uncertain"]["evidence"], "insufficient_evidence")
        self.assertTrue(payload["read_only"])

    def test_scenario_and_evidence_filters_narrow_the_run_set(self) -> None:
        status, payload = self.read("/api/v1/runs?scenario_id=sort-parcels")
        self.assertEqual(status, 200)
        self.assertEqual([run["run_id"] for run in payload["runs"]], ["dashboard-parcel--parcel-intake-003"])
        self.assertEqual((payload["total"], payload["unfiltered_count"]), (1, 4))

        status, payload = self.read("/api/v1/runs?evidence=refuted&page_size=1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["unfiltered_count"], 4)
        self.assertEqual(len(payload["runs"]), 1)

    def test_an_unknown_filter_value_and_an_unusable_page_are_both_refused(self) -> None:
        status, payload = self.read("/api/v1/runs?scenario_id=no-such-scenario")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "unknown_filter_value")
        self.assertEqual(payload["filter"], "scenario_id")

        status, payload = self.read("/api/v1/runs?evidence=succeeded")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "unknown_filter_value")

        for query in ("page_size=0", f"page_size={MAX_RUN_PAGE_SIZE + 1}", "page=0", "page=abc"):
            with self.subTest(query=query):
                status, payload = self.read(f"/api/v1/runs?{query}")
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"], "invalid_filter")

    def test_the_timeline_route_is_ordered_phased_and_bounded(self) -> None:
        status, payload = self.read("/api/v1/runs/run-recovery/timeline")
        self.assertEqual(status, 200)
        self.assertTrue(payload["read_only"])
        items = payload["timeline"]
        self.assertEqual([item["sequence_no"] for item in items], list(range(len(items))))
        self.assertEqual({item["phase"] for item in items} - set(TIMELINE_PHASES), set())
        self.assertEqual([item["phase"] for item in items[:3]], ["context", "context", "observation"])
        self.assertIn("verification", [item["phase"] for item in items])
        self.assertEqual(payload["max_events_per_run"], 10000)

        status, payload = self.read("/api/v1/runs/missing/timeline")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "run_not_found")
        status, payload = self.read("/api/v1/runs/bad%2Fid/timeline")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_run_id")

    def test_write_methods_stay_read_only_on_both_new_routes(self) -> None:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for route in ("/api/v1/runs", "/api/v1/runs/run-recovery/timeline"):
                with self.subTest(method=method, route=route):
                    status, payload = self.read(route, method=method)
                    self.assertEqual(status, 405)
                    self.assertEqual(payload["error"], "read_only")

    def test_the_run_list_response_does_not_leak_an_unbounded_projection(self) -> None:
        status, payload = self.read("/api/v1/runs?page_size=200")
        self.assertEqual(status, 200)
        self.assertEqual(payload["max_page_size"], MAX_RUN_PAGE_SIZE)
        self.assertLessEqual(len(payload["runs"]), MAX_RUN_PAGE_SIZE)
        for values in payload["facets"].values():
            self.assertLessEqual(len(values), MAX_RUN_FACET_VALUES)


class DashboardViewTests(unittest.TestCase):
    """Issue #303 asks for the UI half; these execute the real app.js in Node."""

    def test_filter_options_come_from_the_api_facet_lists(self) -> None:
        options = _run_node(
            "module.exports.filterOptions(JSON.parse(process.argv[1]))",
            {"scenario_id": ["a", "b"], "evidence": ["refuted"]},
        )
        self.assertEqual(options["scenario_id"], ["a", "b"])
        self.assertEqual(options["evidence"], ["refuted"])
        # A facet the API did not report yields an empty list, not a hardcoded default.
        self.assertEqual(options["outcome"], [])
        self.assertEqual(options["scenario_version"], [])
        self.assertEqual(_run_node("module.exports.filterOptions(null)")["evidence"], [])

    def test_the_query_puts_filters_and_paging_on_the_api_request(self) -> None:
        query = _run_node(
            "module.exports.runQuery(JSON.parse(process.argv[1]), 2, 5)",
            {"scenario_id": "sort-parcels", "evidence": "", "outcome": "confirmed"},
        )
        parsed = urllib.parse.parse_qs(query.lstrip("?"))
        self.assertEqual(parsed["scenario_id"], ["sort-parcels"])
        self.assertEqual(parsed["outcome"], ["confirmed"])
        self.assertNotIn("evidence", parsed)
        self.assertEqual(parsed["page"], ["2"])
        self.assertEqual(parsed["page_size"], ["5"])

    def test_local_filtering_matches_the_requested_selection(self) -> None:
        runs = [
            {"run_id": "a", "scenario_id": "sort-parcels", "evidence": "refuted"},
            {"run_id": "b", "scenario_id": "kit-three-parts", "evidence": "confirmed"},
        ]
        self.assertEqual(len(_run_node("module.exports.filterRunList(JSON.parse(process.argv[1]), {})", runs)), 2)
        self.assertEqual(
            [
                run["run_id"]
                for run in _run_node(
                    "module.exports.filterRunList(JSON.parse(process.argv[1]), JSON.parse(process.argv[2]))",
                    runs,
                    {"scenario_id": "sort-parcels"},
                )
            ],
            ["a"],
        )

    def test_an_unknown_or_missing_evidence_reading_is_never_a_success(self) -> None:
        cases = {
            "__missing__": True,
            "": True,
            "future_state": True,
            "failed": True,
            "not_executed": True,
            "insufficient_evidence": True,
            "refuted": False,
            "confirmed": False,
        }
        for state, incomplete in cases.items():
            with self.subTest(state=state):
                run = {} if state == "__missing__" else {"evidence": state}
                reading = _run_node("module.exports.evidenceReading(JSON.parse(process.argv[1]))", run)
                self.assertEqual(reading["incomplete"], incomplete)
                if incomplete:
                    self.assertNotEqual(reading["label"], "已确认")

    def test_an_unresolved_scenario_and_an_unrun_outcome_are_flagged(self) -> None:
        unresolved = _run_node(
            "module.exports.scenarioReading(JSON.parse(process.argv[1]))",
            {"scenario_source": "unresolved", "scenario_id": None},
        )
        self.assertTrue(unresolved["unresolved"])
        resolved = _run_node(
            "module.exports.scenarioReading(JSON.parse(process.argv[1]))",
            {"scenario_source": "registry", "scenario_label": "sort-parcels@0.2"},
        )
        self.assertFalse(resolved["unresolved"])
        self.assertEqual(resolved["label"], "sort-parcels@0.2")

        # `none` is the absence of a verdict, so it is flagged rather than styled
        # like a confirmed result.
        none_outcome = _run_node("module.exports.outcomeReading(JSON.parse(process.argv[1]))", {"outcome": "none"})
        self.assertTrue(none_outcome["unresolved"])
        self.assertEqual(none_outcome["label"], "未验证")

    def test_the_timeline_view_keeps_one_known_phase_per_item(self) -> None:
        items = _run_node(
            "module.exports.timelineView(JSON.parse(process.argv[1]))",
            [
                {"sequence_no": 0, "event_type": "task_accepted", "phase": "context"},
                {"sequence_no": 1, "event_type": "verification", "phase": "verification"},
                {"sequence_no": 2, "event_type": "tool_call", "phase": "invented_phase"},
            ],
        )
        self.assertEqual([item["phase"] for item in items], ["context", "verification", "context"])
        self.assertEqual(items[2]["phase_label"], "上下文")
        self.assertTrue(all(item["phase"] in TIMELINE_PHASES for item in items))

    def test_the_page_summary_reports_that_it_is_a_page(self) -> None:
        summary = _run_node(
            "module.exports.pageSummary(JSON.parse(process.argv[1]))",
            {"total": 3, "unfiltered_count": 40, "page": 2, "page_size": 2},
        )
        self.assertEqual(summary["page_count"], 2)
        self.assertTrue(summary["filtered"])
        self.assertTrue(summary["truncated"])
        single = _run_node(
            "module.exports.pageSummary(JSON.parse(process.argv[1]))",
            {"total": 4, "unfiltered_count": 4, "page": 1, "page_size": 200},
        )
        self.assertFalse(single["filtered"])
        self.assertFalse(single["truncated"])


if __name__ == "__main__":
    unittest.main()
