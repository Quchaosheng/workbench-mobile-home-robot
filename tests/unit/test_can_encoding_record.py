"""Wire V1 is the normative Classic CAN encoding (#578).

`docs/architecture/mcu-wire-v1.md` documented two mutually incompatible encodings
and left the question open: "requires an owner decision recorded under
`docs/decisions/` before any codec changes". Nothing had recorded it, so the
firmware that runs on `MCU-BASE` and the runtime the ROS 2 plane is built from
could not talk to each other.

ADR-0012 records the decision. These tests keep the record, the constants it cites
and the document it updates in agreement — the failure this issue fixes was that
they disagreed, so asserting agreement is the point. They change no codec: the
constants on both sides are asserted to be **unchanged**, which is the evidence
that this issue did not quietly pick a side by editing code.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADR = ROOT / "docs" / "decisions" / "ADR-0012-wire-v1-normative.md"
WIRE_DOC = ROOT / "docs" / "architecture" / "mcu-wire-v1.md"
FIRMWARE_HEADER = ROOT / "firmware" / "mcu" / "core" / "frame_codec.h"
FIRMWARE_TESTS = ROOT / "firmware" / "mcu" / "tests" / "frame_codec_tests.c"
RUNTIME_HEADER = (
    ROOT
    / "integrations"
    / "embodied_agent_runtime"
    / "ros2_ws"
    / "src"
    / "runtime_can"
    / "include"
    / "runtime_can"
    / "protocol.hpp"
)


def adr_text() -> str:
    return " ".join(ADR.read_text(encoding="utf-8").split())


class TestTheDecisionIsRecorded:
    def test_wire_v1_is_named_normative(self) -> None:
        text = adr_text()
        assert "Wire V1 is normative" in text

    def test_the_record_names_all_three_options_and_picks_one(self) -> None:
        """The issue requires one of three options with its consequences."""
        text = adr_text()
        # The chosen option: runtime_can changes, firmware does not.
        assert "runtime_can" in text
        assert "the firmware codec does not change" in text

    def test_the_record_states_which_side_has_the_stronger_evidence(self) -> None:
        text = adr_text()
        assert "golden vector" in text.lower()
        assert "test_protocol.cpp" in text

    def test_the_record_states_the_migration_order(self) -> None:
        text = adr_text()
        assert "Migration order" in text
        # Three staged steps, each named.
        assert "alongside" in text
        assert "deleted in a follow-up" in text

    def test_the_record_answers_the_one_side_updated_case(self) -> None:
        """The issue requires 'the existing fail-closed rejection', not undefined behaviour."""
        text = adr_text()
        assert "fail-closed rejection" in text
        assert "undefined behaviour" in text

    def test_the_record_names_the_follow_up_issue(self) -> None:
        text = adr_text()
        assert "follow-up" in text.lower()
        assert "changes no codec" in text


class TestTheEvidenceCitedIsAccurate:
    """The counts in the ADR are asserted against the files, not trusted."""

    def test_the_golden_vector_count_is_eight(self) -> None:
        source = FIRMWARE_TESTS.read_text(encoding="utf-8")
        match = re.search(r"golden_vectors\[\] = \{(.*?)\n\};", source, re.S)
        assert match, "the golden vector table is missing"
        assert match.group(1).count(".arbitration_id") == 8
        assert "eight golden vectors" in adr_text()

    def test_the_check_call_count_is_thirty_three(self) -> None:
        source = FIRMWARE_TESTS.read_text(encoding="utf-8")
        assert source.count("check(report") == 33
        assert "33 `check()` calls" in adr_text()

    def test_the_wire_v1_constants_are_what_the_record_cites(self) -> None:
        header = FIRMWARE_HEADER.read_text(encoding="utf-8")
        assert "MCU_WIRE_VERSION_V1 0x10u" in header
        assert "MCU_CAN_ID_STOP 0x080u" in header
        assert "MCU_CAN_ID_STOP_ACK 0x081u" in header
        assert "MCU_WIRE_OPCODE_STOP = 5" in header

    def test_the_runtime_constants_are_what_the_record_cites(self) -> None:
        header = RUNTIME_HEADER.read_text(encoding="utf-8")
        assert "kCommandFrameId = 0x100" in header
        assert "kProtocolVersion = 1" in header
        assert "kStopOpcode = 0xFF" in header


class TestNoCodecChanged:
    def test_the_firmware_constants_are_unchanged(self) -> None:
        """The evidence that this issue did not pick a side by editing code."""
        header = FIRMWARE_HEADER.read_text(encoding="utf-8")
        assert "MCU_WIRE_VERSION_V1 0x10u" in header
        assert "MCU_CAN_ID_STOP 0x080u" in header

    def test_the_runtime_constants_are_unchanged(self) -> None:
        """ADR-0012 records the decision; the codec change is the follow-up."""
        header = RUNTIME_HEADER.read_text(encoding="utf-8")
        assert "kStopOpcode = 0xFF" in header
        assert "kProtocolVersion = 1" in header


class TestTheWireDocumentNoLongerLeavesItOpen:
    def test_the_document_points_at_the_adr(self) -> None:
        text = WIRE_DOC.read_text(encoding="utf-8")
        assert "ADR-0012-wire-v1-normative.md" in text

    def test_the_document_no_longer_just_says_a_decision_is_required(self) -> None:
        """The open question is answered; the sentence is past tense now."""
        text = WIRE_DOC.read_text(encoding="utf-8")
        assert "has been made" in text
        # And it still refuses to claim interoperability before the change lands.
        assert "remain incompatible" in text

    def test_the_existing_rejection_test_is_still_there(self) -> None:
        """This issue must not have weakened the cross-codec rejection."""
        test_file = ROOT / "tests" / "unit" / "test_can_transport.py"
        assert "test_rejects_a_foreign_classic_can_encoding" in test_file.read_text(encoding="utf-8")


class TestTheRecordIsReachable:
    def test_the_adr_is_in_the_navigation(self) -> None:
        nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
        assert "decisions/ADR-0012-wire-v1-normative.md" in nav

    def test_the_adr_has_the_expected_front_matter(self) -> None:
        text = ADR.read_text(encoding="utf-8")
        assert re.search(r"^Status: \*\*accepted\*\*$", text, re.M)
