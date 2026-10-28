from __future__ import annotations

import csv
import importlib.util
import json
import multiprocessing
import re
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "libs" / "task_utils"))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _register_evidence_process(module_path, register, root, record, barrier, results) -> None:
    module = load_module("validation_evidence_process", module_path)
    barrier.wait()
    try:
        module.register(
            register,
            record,
            root=root,
            scenarios={"VAL5-01"},
            units={"UNIT-001": ("EVT1", "a" * 64)},
        )
    except module.EvidenceError as exc:
        results.put(("rejected", str(exc)))
    else:
        results.put(("accepted", ""))


class MechanicalPackageTests(unittest.TestCase):
    def test_analysis_passes_and_is_explicitly_analytical(self) -> None:
        module = load_module("mechanical_generator", ROOT / "hardware/mechanical/tools/generate_artifacts.py")
        report = module.analyse()
        self.assertTrue(all(report["checks"].values()))
        self.assertTrue(all(report["geometry_checks"].values()))
        self.assertGreaterEqual(report["static_tip_angle_deg"], 35)
        self.assertGreaterEqual(report["load_cases"]["navigation_low"]["tip_angle_deg"], 25)
        self.assertGreaterEqual(report["load_cases"]["stabilized_manipulation"]["tip_angle_deg"], 35)
        self.assertGreater(report["arm_plus_payload_screen_moment_nm"], report["payload_only_moment_nm"])
        self.assertGreater(
            report["bimanual_shared_workspace_screen_moment_nm"], report["arm_plus_payload_screen_moment_nm"]
        )
        self.assertEqual(report["status"], "CONCEPT_PHYSICAL_VALIDATION_REQUIRED")
        self.assertIn("PHYSICAL_VALIDATION_REQUIRED", report["status"])

    def test_step_exchange_file_has_header_and_solid_body(self) -> None:
        step = (ROOT / "hardware/mechanical/generated/enclosure.step").read_text(encoding="ascii")
        self.assertTrue(step.startswith("ISO-10303-21;"))
        self.assertIn("END-ISO-10303-21;", step)
        self.assertIn("MANIFOLD_SOLID_BREP", step)

    def test_assembly_parts_drawings_and_drop_screen_are_present(self) -> None:
        generated = ROOT / "hardware/mechanical/generated"
        self.assertGreater((generated / "desk_robot_assembly.step").stat().st_size, 100_000)
        self.assertGreater((generated / "desk_robot_navigation_low.step").stat().st_size, 100_000)
        self.assertGreater((generated / "desk_robot_exploded.step").stat().st_size, 100_000)
        self.assertEqual(len(list((generated / "parts").glob("*.step"))), 10)
        mobile_base_step = (generated / "parts/mobile_base.step").read_text(encoding="ascii")
        self.assertGreaterEqual(mobile_base_step.count("MANIFOLD_SOLID_BREP"), 17)
        neck_step = (generated / "parts/neck_mount.step").read_text(encoding="ascii")
        self.assertIn("MANIFOLD_SOLID_BREP", neck_step)
        sequence = json.loads((generated / "assembly-sequence.json").read_text(encoding="utf-8"))
        self.assertEqual([item["part"] for item in sequence[-3:]], ["neck_mount", "head_module", "tool_dock"])
        self.assertTrue((generated / "drawings/general-arrangement.svg").exists())
        screening = json.loads((generated / "drop-screening.json").read_text(encoding="utf-8"))
        self.assertEqual(screening["acceptance"]["peak_deceleration_g"], 20)

    def test_controller_board_fits_tray_and_mount_pattern_is_controlled(self) -> None:
        module = load_module("mechanical_generator_fit", ROOT / "hardware/mechanical/tools/generate_artifacts.py")
        report = module.analyse()
        self.assertTrue(report["checks"]["pcb_fits_electronics_tray"])
        self.assertTrue(report["checks"]["pcb_edge_service_margin_met"])
        self.assertEqual(report["pcb_tray_margin_mm"], [140, 130])
        self.assertEqual(report["pcb_edge_service_margin_mm"], [70, 65])
        spec = json.loads((ROOT / "hardware/mechanical/design-spec.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["electronics_tray"]["pcb_mount_pattern"], [152, 122])

    def test_revision_d_bimanual_home_robot_mechanics_are_explicit_and_aligned(self) -> None:
        spec = json.loads((ROOT / "hardware/mechanical/design-spec.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["revision"], "D")
        self.assertEqual(spec["enclosure"]["width"], 540)
        self.assertEqual(spec["enclosure"]["depth"], 520)
        self.assertEqual(spec["lifting_platform"]["travel"], 350)
        self.assertEqual(spec["chassis"]["stabilizer_count"], 4)
        self.assertEqual(spec["chassis"]["drive_architecture"], "four_module_independent_steer_and_drive")
        self.assertTrue(spec["chassis"]["holonomic_motion"])
        self.assertEqual(spec["chassis"]["drive_module_count"], 4)
        self.assertEqual(spec["chassis"]["wheel_diameter_mm"], 140)
        self.assertEqual(spec["chassis"]["suspension_travel_mm"], 30)
        self.assertIn("SOFTWARE_AND_PHYSICAL_VALIDATION_REQUIRED", spec["chassis"]["autonomous_navigation_status"])
        self.assertEqual(
            set(spec["chassis"]["motion_modes"]), {"longitudinal", "lateral", "diagonal", "rotate_in_place"}
        )
        self.assertEqual(spec["manipulator"]["count"], 2)
        self.assertEqual(spec["manipulator"]["total_revolute_axes"], 14)
        self.assertEqual(spec["head"]["display_shape"], "rounded_rectangle")
        self.assertEqual(spec["head"]["display_cutout"], [228, 92])
        self.assertEqual(spec["head"]["display_corner_radius_mm"], 24)
        neck_mount = spec["head"]["neck_mount"]
        self.assertEqual(neck_mount["type"], "keyed_pedestal_with_hidden_bolted_flange")
        self.assertEqual(neck_mount["fasteners"], "4x M6 captive from underside + 2x dowel pins")
        self.assertEqual(neck_mount["cable_passage_mm"], 32)
        self.assertEqual(len(spec["manipulator"]["joints"]), 7)
        self.assertEqual([joint["id"] for joint in spec["manipulator"]["joints"]], [f"J{i}" for i in range(1, 8)])
        arm_design = spec["manipulator"]["industrial_design"]
        self.assertEqual(arm_design["shoulder_module"], "torso_recessed_three_axis_yoke")
        self.assertEqual(arm_design["upper_arm_shell"], "tapered_rounded_rect_fairing")
        self.assertEqual(arm_design["nominal_motion_gap_mm"], 6)
        self.assertEqual(spec["tool_system"]["interface"], "kinematic_quick_change_with_mechanical_lock")
        scad = (ROOT / "hardware/mechanical/cad/desk_robot.scad").read_text(encoding="utf-8")
        self.assertIn("STABILIZERS_DEPLOYED = false", scad)
        for feature in (
            "mobile_base",
            "lifting_platform",
            "seven_axis_arm",
            "dual_seven_axis_arms",
            "head_and_face",
            "neck_mount",
            "tool_dock",
        ):
            self.assertIn(f"module {feature}", scad)
        drawing = (ROOT / "hardware/mechanical/generated/drawings/general-arrangement.svg").read_text(encoding="utf-8")
        self.assertIn("REV D", drawing)
        self.assertIn("Dual seven-axis arms", drawing)
        self.assertIn("keyed bolted neck mount", drawing)

    def test_generated_step_height_matches_revision_d_envelope(self) -> None:
        report = json.loads((ROOT / "hardware/mechanical/generated/analysis.json").read_text(encoding="utf-8"))
        bounds = report["generated_geometry"]["assembly_bounds_mm"]
        navigation_bounds = report["generated_geometry"]["navigation_bounds_mm"]
        spec = json.loads((ROOT / "hardware/mechanical/design-spec.json").read_text(encoding="utf-8"))
        self.assertEqual(report["generated_geometry"]["status"], "MEASURED")
        self.assertAlmostEqual(bounds["zmin_mm"], spec["coordinate_system"]["ground_plane_z_mm"], delta=0.1)
        self.assertAlmostEqual(
            bounds["zmax_mm"],
            spec["enclosure"]["height"],
            delta=spec["coordinate_system"]["height_tolerance_mm"],
        )
        self.assertAlmostEqual(
            navigation_bounds["zmax_mm"],
            spec["enclosure"]["stowed_height"],
            delta=spec["coordinate_system"]["height_tolerance_mm"],
        )
        self.assertLessEqual(navigation_bounds["xmax_mm"] - navigation_bounds["xmin_mm"], spec["chassis"]["width"])
        self.assertLessEqual(navigation_bounds["ymax_mm"] - navigation_bounds["ymin_mm"], spec["chassis"]["depth"])
        self.assertGreaterEqual(bounds["xmax_mm"], spec["chassis"]["stabilized_support_width"] / 2)
        self.assertEqual(report["geometry_reference"]["assembly_pose"], "raised_stabilized")


class PcbPackageTests(unittest.TestCase):
    def test_power_budget_and_protection_checks_pass(self) -> None:
        module = load_module("electrical_checks", ROOT / "hardware/pcb/tools/electrical_checks.py")
        report = module.calculate()
        self.assertTrue(report["pass"])
        self.assertLess(report["input_current_at_36v_a"], 10)
        self.assertIn("LAB_VALIDATION_REQUIRED", report["status"])
        self.assertEqual(set(report["load_cases"]), {"JETSON_15W", "JETSON_25W", "JETSON_40W_MAXN"})
        self.assertEqual(set(report["input_corner_currents_a"]), {"36V", "48V", "60V"})

    def test_every_external_connector_is_keyed_or_test_only(self) -> None:
        with (ROOT / "hardware/pcb/connectors.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertGreaterEqual(len(rows), 10)
        self.assertTrue(all(row["keying"] in {"mandatory", "polarized", "red keyed", "n/a"} for row in rows))

    def test_board_declares_six_copper_layers_and_real_footprints(self) -> None:
        board = (ROOT / "hardware/pcb/kicad/controller.kicad_pcb").read_text(encoding="utf-8")
        copper_layers = re.findall(r'^\s*\(\d+ "(?:F|B|In\d+)\.Cu" signal\)$', board, flags=re.MULTILINE)
        self.assertEqual(len(copper_layers), 8)
        self.assertGreaterEqual(board.count("(footprint "), 29)
        self.assertGreaterEqual(board.count("(segment"), 168)
        self.assertGreaterEqual(board.count("(via"), 8)
        for signal in [
            "SPI_SCLK",
            "MOTOR_ENABLE_REQ",
            "MOTOR_ENABLE_SAFE",
            "MOTOR_CS5",
            "I2C_SDA",
            "ESTOP_SENSE",
            "MCU_RESET",
        ]:
            self.assertIn(signal, board)
        for isolated_net in ["5V_CAN_ISO", "GND_CAN_ISO", "CAN_TX", "CAN_RX"]:
            self.assertIn(isolated_net, board)
        self.assertIn('property "Reference" "J4"', board)
        self.assertIn('property "Reference" "U7"', board)
        self.assertIn('property "Reference" "U8"', board)
        self.assertIn('property "Reference" "J11"', board)

    def test_pinout_and_release_audit_prevent_unsafe_order_release(self) -> None:
        module = load_module("release_readiness", ROOT / "hardware/pcb/tools/release_readiness.py")
        report = module.audit()
        self.assertTrue(report["engineering_package_pass"])
        self.assertEqual(report["status"], "PRODUCTION_RELEASE_BLOCKED")
        self.assertTrue(report["order_release_checks"]["detailed_schematic_has_symbols"])
        self.assertFalse(report["order_release_checks"]["safety_analysis_approved"])
        self.assertTrue(report["engineering_checks"]["approval_register_covers_all_pending_bom_lines"])
        self.assertEqual(len(report["procurement_hold_references"]), 68)
        self.assertTrue(report["engineering_checks"]["safety_truth_table_covers_channel_discrepancy"])
        with (ROOT / "hardware/pcb/connector-pinout.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len([row for row in rows if row["reference"] == "J4"]), 20)
        self.assertEqual(len([row for row in rows if row["reference"] == "J10"]), 4)
        self.assertEqual(len([row for row in rows if row["reference"] == "J11"]), 4)
        self.assertEqual(len([row for row in rows if row["reference"] == "J2"]), 4)
        self.assertEqual(len([row for row in rows if row["reference"] == "J5"]), 4)
        self.assertEqual(len([row for row in rows if row["reference"] == "J6"]), 4)
        with (ROOT / "hardware/pcb/component-selection-matrix.csv").open(newline="", encoding="utf-8") as handle:
            components = list(csv.DictReader(handle))
        self.assertEqual(
            {row["reference"] for row in components},
            {"D2", "F2", "K1 K2", "L1", "Q1 Q2", "RS1", "U1", "U2", "U3", "U4", "U5", "U6", "U7", "U8"},
        )
        connectivity = json.loads(
            (ROOT / "hardware/pcb/generated/connectivity_report.json").read_text(encoding="utf-8")
        )
        self.assertTrue(connectivity["pass"])
        self.assertEqual(connectivity["checked_pin_count"], 458)
        board = (ROOT / "hardware/pcb/kicad/controller.kicad_pcb").read_text(encoding="utf-8")
        self.assertTrue(all(f"TP{index}" in board for index in range(1, 9)))

    def test_wiring_keeps_current_j11_separate_from_future_dual_channel_safety(self) -> None:
        wiring = (ROOT / "docs/hardware/wiring.md").read_text(encoding="utf-8")
        self.assertIn("J11 single-channel diagnostic output only", wiring)
        self.assertIn("Future safety ECO: Dual-channel E-stop -> J10 -> K1/K2 -> H09 -> childboard J_SAFE", wiring)
        self.assertNotIn("K1/K2 safety chain -> J11", wiring)

    def test_official_sources_and_interface_freeze_states_are_explicit(self) -> None:
        baseline = json.loads((ROOT / "hardware/pcb/source-baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(baseline["maturity"], "EVT_REVIEWABLE_NOT_PRODUCTION_RELEASED")
        self.assertGreaterEqual(len(baseline["sources"]), 6)
        self.assertTrue(all(item["url"].startswith("https://") for item in baseline["sources"]))
        required = {"confidence", "freeze_status", "owner"}
        self.assertTrue(all(required <= item.keys() for item in baseline["sources"]))
        self.assertTrue(all(required <= item.keys() for item in baseline["controlled_assumptions"]))

    def test_kicad_cli_release_reports_are_clean_and_fabrication_exists(self) -> None:
        pcb = ROOT / "hardware/pcb"
        drc = (pcb / "generated/drc.rpt").read_text(encoding="utf-8")
        erc = (pcb / "generated/erc.rpt").read_text(encoding="utf-8")
        self.assertIn("Found 0 DRC violations", drc)
        self.assertIn("Found 0 unconnected pads", drc)
        self.assertIn("0  Errors 0  Warnings", erc)
        gerbers = list((pcb / "fabrication/gerbers").glob("controller-*"))
        self.assertGreaterEqual(len(gerbers), 15)
        self.assertTrue((pcb / "fabrication/controller.d356").exists())

    def test_project_enforces_documented_dfm_minimums(self) -> None:
        project = json.loads((ROOT / "hardware/pcb/kicad/controller.kicad_pro").read_text(encoding="utf-8"))
        rules = project["board"]["design_settings"]["rules"]
        self.assertEqual(rules["min_clearance"], 0.15)
        self.assertEqual(rules["min_track_width"], 0.15)
        self.assertEqual(rules["min_through_hole_diameter"], 0.3)
        self.assertEqual(rules["min_via_annular_width"], 0.15)


class ManufacturingPackageTests(unittest.TestCase):
    def test_route_is_ordered_timed_and_gated(self) -> None:
        module = load_module("route_checks", ROOT / "hardware/manufacturing/tools/validate_route.py")
        report = module.validate()
        self.assertTrue(report["pass"])
        self.assertEqual(report["operation_count"], 14)

    def test_harness_spec_has_calculated_drop_and_release_holds(self) -> None:
        module = load_module("harness_checks", ROOT / "hardware/manufacturing/tools/validate_harnesses.py")
        report = module.validate()
        self.assertTrue(report["engineering_package_pass"])
        self.assertEqual(report["status"], "HARNESS_RELEASE_BLOCKED")
        self.assertEqual(len(report["results"]), 8)
        self.assertTrue(all(item["drop_pass"] for item in report["results"]))

    def test_pilot_template_does_not_claim_units_were_built(self) -> None:
        pilot = (ROOT / "hardware/manufacturing/pilot-and-release.md").read_text(encoding="utf-8")
        self.assertIn("NOT BUILT", pilot)
        self.assertIn("NO-GO", pilot)

    def test_generated_pilot_has_twenty_serials_and_controlled_drawings(self) -> None:
        generated = ROOT / "hardware/manufacturing/generated"
        with (generated / "pilot-log.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 20)
        self.assertTrue(all(row["final_result"] == "NOT_BUILT" for row in rows))
        self.assertTrue((generated / "line-layout.svg").exists())
        self.assertTrue((generated / "fixture-drawings.svg").exists())
        self.assertTrue((generated / "packaging-drawing.svg").exists())


class ProcurementPackageTests(unittest.TestCase):
    def test_procurement_package_is_complete_without_fake_quotes(self) -> None:
        module = load_module("procurement_checks", ROOT / "hardware/procurement/tools/validate_procurement.py")
        report = module.validate()
        self.assertTrue(report["pass"])
        self.assertEqual(report["status"], "ORDER_RELEASE_BLOCKED")
        self.assertGreaterEqual(report["quote_request_count"], 2 * 4)
        self.assertTrue(all(row["unit_cost_usd"] == "" for row in module.read_csv("bom.csv")))


class QualityPackageTests(unittest.TestCase):
    def test_quality_package_has_fmea_and_explicit_execution_gates(self) -> None:
        module = load_module("qa_checks", ROOT / "hardware/qa/tools/validate_qa.py")
        report = module.validate()
        self.assertTrue(report["pass"])
        self.assertEqual(report["status"], "EXECUTION_REQUIRED")
        self.assertEqual(report["physical_results"], "NOT_EXECUTED")


class ValidationPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)
        self.raw = self.root / "scope.csv"
        self.raw.write_text("time,current\n0,0\n", encoding="utf-8")
        self.module_path = ROOT / "hardware/validation/tools/evidence.py"
        self.module = load_module("validation_evidence", self.module_path)
        self.register = self.root / "register.jsonl"
        self.kwargs = {
            "root": self.root,
            "scenarios": {"VAL5-01"},
            "units": {"UNIT-001": ("EVT1", "a" * 64)},
        }

    def _record(self, evidence_id: str) -> dict:
        return {
            "evidence_id": evidence_id,
            "scenario_id": "VAL5-01",
            "unit_id": "UNIT-001",
            "hardware_revision": "EVT1",
            "config_hash": "a" * 64,
            "operator": "operator",
            "reviewer": "reviewer",
            "captured_at": "2026-08-18T08:00:00Z",
            "evidence_kind": "physical",
            "instrument_refs": ["SCOPE-01"],
            "calibration_refs": ["CAL-01"],
            # A physical record has to say which instrument produced the numbers and
            # that it was in calibration when it did (issue #444).
            "instrument_serials": ["SN-SCOPE-01"],
            "calibration_due": {"SN-SCOPE-01": "2027-01-01T00:00:00Z"},
            "measurement_summary": {"offset_mv": 1.5},
            "raw_files": {"scope.csv": self.module.sha256(self.raw)},
            "raw_file_kinds": {"scope.csv": "capture"},
            "result": "PASS",
        }

    def test_validation_package_has_fault_library_and_no_fake_units(self) -> None:
        module = load_module("validation_checks", ROOT / "hardware/validation/tools/validate_validation.py")
        report = module.validate()
        self.assertTrue(report["pass"])
        self.assertEqual(report["fault_scenario_count"], 20)
        self.assertEqual(report["first_batch_unit_count"], 10)
        self.assertEqual(report["physical_results"], "NOT_EXECUTED")

    def test_evidence_registration_fails_closed_and_accepts_valid_records(self) -> None:
        base = self._record("EVIDENCE-001")
        self.module.register(self.register, base, **self.kwargs)
        self.assertEqual(self.module.validate_register(self.register, **self.kwargs), [base])
        with self.assertRaisesRegex(self.module.EvidenceError, "duplicate"):
            self.module.register(self.register, base, **self.kwargs)
        with self.assertRaisesRegex(self.module.EvidenceError, "revision mismatch"):
            self.module.validate_record({**base, "hardware_revision": "EVT2"}, **self.kwargs)
        self.raw.unlink()
        with self.assertRaisesRegex(self.module.EvidenceError, "missing"):
            self.module.validate_register(self.register, **self.kwargs)

    def test_concurrent_duplicate_evidence_id_has_one_process_winner(self) -> None:
        record = self._record("EVIDENCE-DUPLICATE")
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        results = context.Queue()
        processes = [
            context.Process(
                target=_register_evidence_process,
                args=(self.module_path, self.register, self.root, record, barrier, results),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join()
            self.assertEqual(process.exitcode, 0)
        outcomes = [results.get(timeout=2) for _ in processes]
        self.assertEqual(sorted(status for status, _ in outcomes), ["accepted", "rejected"])
        self.assertIn("duplicate evidence_id", next(message for status, message in outcomes if status == "rejected"))
        self.assertEqual(self.module.validate_register(self.register, **self.kwargs), [record])

    def test_concurrent_distinct_evidence_ids_are_both_retained(self) -> None:
        barrier = threading.Barrier(2)

        def register_record(record: dict) -> None:
            barrier.wait()
            self.module.register(self.register, record, **self.kwargs)

        records = [self._record(f"EVIDENCE-{index}") for index in range(2)]
        with ThreadPoolExecutor(max_workers=2) as executor:
            for future in [executor.submit(register_record, record) for record in records]:
                future.result(timeout=10)
        actual = self.module.validate_register(self.register, **self.kwargs)
        self.assertEqual({record["evidence_id"] for record in actual}, {"EVIDENCE-0", "EVIDENCE-1"})

    def test_failed_append_restores_the_valid_register(self) -> None:
        first = self._record("EVIDENCE-001")
        self.module.register(self.register, first, **self.kwargs)
        original = self.register.read_bytes()
        write = self.module.os.write

        def short_write(descriptor: int, payload: bytes) -> int:
            return write(descriptor, payload[: len(payload) // 2])

        with mock.patch.object(self.module.os, "write", side_effect=short_write):
            with self.assertRaisesRegex(OSError, "short write"):
                self.module.register(self.register, self._record("EVIDENCE-002"), **self.kwargs)
        self.assertEqual(self.register.read_bytes(), original)
        self.assertEqual(self.module.validate_register(self.register, **self.kwargs), [first])

        third = self._record("EVIDENCE-003")
        self.module.register(self.register, third, **self.kwargs)
        self.assertEqual(self.module.validate_register(self.register, **self.kwargs), [first, third])

    def test_physical_result_requires_physical_pass_for_every_scenario(self) -> None:
        module = load_module("validation_result_derivation", ROOT / "hardware/validation/tools/validate_validation.py")
        scenarios = {"VAL5-01", "VAL5-02"}
        simulation = [{"scenario_id": scenario, "result": "PASS"} for scenario in scenarios]
        self.assertEqual(set(module.derive_results(scenarios, simulation).values()), {"PASS"})
        physical = [{"scenario_id": "VAL5-01", "result": "PASS"}]
        self.assertEqual(module.derive_results(scenarios, physical)["VAL5-02"], "NOT_EXECUTED")


class ReleaseReadinessTests(unittest.TestCase):
    def test_release_register_is_fail_closed_and_cross_package(self) -> None:
        module = load_module("release_readiness_checks", ROOT / "hardware/release/tools/check_release_readiness.py")
        report = module.validate()
        self.assertTrue(report["pass"])
        self.assertEqual(report["status"], "PRODUCTION_RELEASE_BLOCKED")
        self.assertGreaterEqual(report["blocker_count"], 10)
        self.assertIn("REL-004", report["blockers"])
        self.assertIn("REL-014", report["blockers"])

    def test_current_hardware_selection_uses_revision_d_mobility(self) -> None:
        selection = (ROOT / "hardware/release/recommended-selection-package.md").read_text(encoding="utf-8")
        closure = (ROOT / "hardware/release/selection-closure-register.csv").read_text(encoding="utf-8")
        power = json.loads((ROOT / "hardware/release/full-system-power-architecture.json").read_text(encoding="utf-8"))
        self.assertIn("Revision D", selection)
        self.assertIn("four independent steer-drive modules", selection.lower())
        self.assertIn("four 140mm wheel", closure)
        self.assertNotIn("two 200 mm driven wheels", selection)
        self.assertEqual(
            power["operating_modes"]["TRANSPORT"]["allowed_high_power_loads"],
            ["four_module_steer_drive_system"],
        )
        self.assertIn("four_module_steer_drive_system", power["controller_u2_prohibited_loads"])


class TaskPacketTests(unittest.TestCase):
    def test_task_packet_limits_writes_to_hardware_and_tests(self) -> None:
        packet = json.loads(
            (ROOT / "docs/task_packets/hardware-engineering-019-021-023.json").read_text(encoding="utf-8")
        )
        self.assertEqual(packet["issue"], "19,21,23")
        self.assertEqual(packet["issues"], [19, 21, 23])
        self.assertIn("robot/control/**", packet["forbidden"])
        self.assertIn("firmware/**", packet["forbidden"])

    def test_procurement_quality_validation_packet_is_bounded(self) -> None:
        packet = json.loads(
            (ROOT / "docs/task_packets/hardware-engineering-022-024-028.json").read_text(encoding="utf-8")
        )
        self.assertEqual(packet["issues"], [22, 24, 28])
        self.assertIn("hardware/procurement/**", packet["allowed_paths"])
        self.assertIn("hardware/qa/**", packet["allowed_paths"])
        self.assertIn("hardware/validation/**", packet["allowed_paths"])
        self.assertIn("firmware/**", packet["forbidden"])

    def test_release_readiness_packet_is_bounded(self) -> None:
        packet = json.loads(
            (ROOT / "docs/task_packets/hardware-engineering-release-readiness.json").read_text(encoding="utf-8")
        )
        self.assertEqual(packet["issues"], [19, 22, 23, 24, 28])
        self.assertIn("hardware/release/**", packet["allowed_paths"])
        self.assertIn("interfaces/**", packet["forbidden"])


if __name__ == "__main__":
    unittest.main()
