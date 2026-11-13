"""The virtual MCU is a legacy stub, and one parity claim is now settled (#587).

`firmware/mcu/README.md` says `workbench_virtual_mcu` "is retired as a safety
reference and parity oracle. It remains only as a legacy compatibility stub for
earlier Python consumers and is not evidence of C protocol, firmware, or physical
safety behavior."

That sentence was the only place the boundary existed. The module was still the
thing the demo, the property suite, the mutation registry and one assurance test
exercised, and nothing in a gate's output said the module was anything other than
the reference. A green property gate naming `workbench_virtual_mcu` reads as
firmware evidence unless the artifact says otherwise.

This module does two things:

* it compares the stub against the C implementation for the transitions both
  implement, so the word "parity" is either earned or dropped;
* it records the disagreement it finds as a **finding**, not as a bug fixed here.
  The stub is not the reference, so the C behaviour is the authority and the stub
  keeps its own behaviour until a consumer needs it changed.

The comparison reads `firmware/mcu/core/state_machine.c` rather than calling it,
because building and running the freestanding target needs an ARM toolchain that a
Python test run does not have. Reading the source is enough to pin the *decision*
table, which is what parity would mean: the same state for the same event.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
C_SOURCE = ROOT / "firmware/mcu/core/state_machine.c"
README = ROOT / "firmware/mcu/README.md"
STUB = ROOT / "firmware/virtual_mcu/workbench_virtual_mcu/state_machine.py"

sys.path.insert(0, str(ROOT / "firmware/virtual_mcu"))

from workbench_virtual_mcu.state_machine import McuState, VirtualMcu


def _c_case(source: str, event: str) -> str:
    """The body of one C `case MCU_EVENT_*` block, up to the next case.

    Slicing to the next `case` rather than to the first `return;` matters: every
    branch of these handlers returns, so stopping at the first one would compare a
    guard clause instead of the decision.
    """
    start = source.find(f"case {event}:")
    assert start != -1, f"the C state machine has no {event} case"
    rest = source[start:]
    following = rest.find("    case MCU_EVENT_", 1)
    return rest if following == -1 else rest[:following]


def _c_stop_handling() -> str:
    """The C `MCU_EVENT_STOP` case, as source text."""
    return _c_case(C_SOURCE.read_text(encoding="utf-8"), "MCU_EVENT_STOP")


class TestTheLabelIsInTheArtifacts:
    def test_the_module_docstring_says_legacy_and_not_safety_evidence(self) -> None:
        """The same words the README uses, in the module a reader opens."""
        text = STUB.read_text(encoding="utf-8")
        assert "legacy" in text.lower()
        assert "NOT safety evidence" in text
        # The authority pointer, so the next reader does not rediscover it.
        assert "firmware/mcu/README.md" in text
        assert "firmware/mcu/core/state_machine.c" in text

    def test_the_gate_archives_label_the_module_legacy(self) -> None:
        """A green gate must not be quotable as firmware evidence."""
        import importlib.util

        spec = importlib.util.spec_from_file_location("quality_gates", ROOT / "tools" / "scripts" / "quality_gates.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        # `quality_gates` imports `_paths` from its own directory, and dataclasses
        # resolves type annotations through `sys.modules`, so the module has to be
        # registered before it executes.
        sys.path.insert(0, str(ROOT / "tools" / "scripts"))
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        labels = module.MODULE_LABELS
        assert labels["workbench_virtual_mcu.state_machine"] == "legacy"
        assert labels["firmware/virtual_mcu"] == "legacy"
        # A module that is genuinely authoritative must not be labelled legacy.
        assert labels.get("workbench.kernel.lifecycle") != "legacy"

    @pytest.mark.parametrize(
        "consumer",
        [
            "tools/scripts/demo_scripted.py",
            "tests/property/test_property_state_transitions.py",
        ],
    )
    def test_every_consumer_carries_the_pointer(self, consumer: str) -> None:
        """Each importer names the authority paragraph, so the boundary travels."""
        text = (ROOT / consumer).read_text(encoding="utf-8")
        assert "firmware/mcu/README.md" in text, f"{consumer} does not point at the authority paragraph"


class TestParityIsEitherEarnedOrDropped:
    def test_stop_from_fault_is_a_recorded_disagreement(self) -> None:
        """The finding: the C dispatch rejects STOP in FAULT; the stub accepts it.

        This is asserted in both directions, so neither side can change quietly
        and leave the other describing a behaviour it no longer has.
        """
        # The C side, read from the source: STOP in FAULT is rejected.
        c_case = _c_stop_handling()
        assert "MCU_STATE_FAULT" in c_case
        assert "MCU_RESULT_REJECTED" in c_case
        assert "MCU_REASON_STOP_REJECTED" in c_case

        # The stub side: STOP from FAULT is accepted and lands in SAFE_STOP.
        stub = VirtualMcu()
        stub.watchdog_timeout()
        assert stub.state is McuState.FAULT
        result = stub.command("stop")
        assert result.accepted is True
        assert stub.state is McuState.SAFE_STOP

    def test_the_disagreement_is_written_down_in_the_module(self) -> None:
        """A finding only counts if the artifact says so, not just this test."""
        text = STUB.read_text(encoding="utf-8")
        assert "not in parity" in text.lower() or "not** in parity" in text.lower()
        assert "MCU_FAULT_STOP_REJECTED" in text

    def test_the_transitions_that_do_agree(self) -> None:
        """The subset where both implementations agree, so parity is not a slogan."""
        # BEGIN_MOVE from IDLE -> EXECUTING, and COMPLETE from EXECUTING -> IDLE.
        stub = VirtualMcu()
        assert stub.command("execute").accepted is True
        assert stub.state is McuState.EXECUTING
        assert stub.command("complete").accepted is True
        assert stub.state is McuState.IDLE

        # The C dispatch implements the same two: BEGIN_MOVE requires IDLE and
        # enters EXECUTING; COMPLETE requires EXECUTING and enters IDLE.
        source = C_SOURCE.read_text(encoding="utf-8")
        begin = _c_case(source, "MCU_EVENT_BEGIN_MOVE")
        complete = _c_case(source, "MCU_EVENT_COMPLETE")
        assert "MCU_STATE_IDLE" in begin
        assert "enter_executing" in begin
        assert "MCU_STATE_EXECUTING" in complete
        assert "enter_idle" in complete

    def test_execute_from_executing_is_rejected_by_both(self) -> None:
        """A rejection both sides share, so the agreement is not only happy paths."""
        stub = VirtualMcu()
        stub.command("execute")
        assert stub.command("execute").accepted is False

        source = C_SOURCE.read_text(encoding="utf-8")
        assert "reject_start" in _c_case(source, "MCU_EVENT_BEGIN_MOVE")


class TestTheREADMEKeepsTheAuthorityParagraph:
    def test_the_readme_still_states_the_boundary(self) -> None:
        """The paragraph wraps, so compare on collapsed whitespace."""
        text = " ".join(README.read_text(encoding="utf-8").split())
        assert "retired as a safety reference and parity oracle" in text
        assert "legacy compatibility stub" in text
