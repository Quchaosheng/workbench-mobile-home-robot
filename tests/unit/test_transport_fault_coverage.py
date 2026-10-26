import sys

sys.path[:0] = ["tools/scripts"]
import scenario_tools
from workbench.application.fault_injection import (
    SafeBehavior,
    coverage_report,
    declared_faults,
    fault_spec,
)
from workbench.hardware import CanDiagnosticCode


def test_transport_faults_are_declared_runtime_faults():
    declared = declared_faults()
    for fault in ("ack_timeout", "stop_timeout", "stop_rejected"):
        assert fault in declared
        # A transport fault is a property of the bus, never of one scenario file.
        assert fault not in scenario_tools.P2_FAULT_TYPES


def test_each_transport_fault_matches_a_shipped_diagnostic_code():
    # The declared fault must be observable through the code the bus actually emits,
    # so the coverage claim is checkable instead of aspirational.
    for fault in ("ack_timeout", "stop_timeout", "stop_rejected"):
        spec = fault_spec(fault)
        assert spec.expected_safe_behaviour is SafeBehavior.MCU_SAFE_STATE
        assert spec.evidence_requirement.startswith(f"CanDiagnosticCode.{fault.upper()}")
        assert fault in {code.value for code in CanDiagnosticCode}


def test_coverage_report_does_not_promote_an_unexecuted_transport_fault():
    report = coverage_report(simulated=sorted(declared_faults()))
    for fault in ("ack_timeout", "stop_timeout", "stop_rejected"):
        row = next(item for item in report["faults"] if item["fault"] == fault)
        assert row["status"] == "SIMULATED"
        assert row["hardware_tested"] is False
