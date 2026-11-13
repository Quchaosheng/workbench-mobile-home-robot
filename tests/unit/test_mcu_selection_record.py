"""The MCU selection record must match the board the BSP declares (#574).

ADR-0003 selected RISC-V rv32imac and named the CH32V307 as the safety MCU and
reference part. `bsp/board-manifest.yaml` names `STM32G474RET6` and `STM32G0B1`,
and `firmware/mcu/hal/ch32v307/` is an empty placeholder. An accepted ADR that
names a part the board does not carry is a stale claim on the critical path of the
firmware effort, not a decision record.

These tests keep the three artifacts agreeing — the ADR, the manifest and the
firmware README — because the failure this issue fixes was exactly that they did
not. They read the files; they do not build firmware, and selecting a part is not
building it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADR_0011 = ROOT / "docs" / "decisions" / "ADR-0011-stm32-mcu-selection.md"
ADR_0003 = ROOT / "docs" / "decisions" / "ADR-0003-mcu-riscv-qemu.md"
MANIFEST = ROOT / "bsp" / "board-manifest.yaml"
FIRMWARE_README = ROOT / "firmware" / "mcu" / "README.md"


def manifest_text() -> str:
    return MANIFEST.read_text(encoding="utf-8")


class TestThePartsTheAdrNamesAreThePartsTheBoardCarries:
    def test_the_manifest_names_the_two_parts(self) -> None:
        text = manifest_text()
        assert "STM32G474RET6" in text
        assert "STM32G0B1" in text

    def test_the_adr_names_the_same_two_parts(self) -> None:
        """A record naming a different part than the board is the defect this fixes."""
        adr = ADR_0011.read_text(encoding="utf-8")
        assert "STM32G474RET6" in adr
        assert "STM32G0B1" in adr

    def test_the_adr_says_both_are_fdcan(self) -> None:
        adr = ADR_0011.read_text(encoding="utf-8")
        assert adr.count("FDCAN") >= 2

    def test_the_adr_records_the_independent_reset_domain(self) -> None:
        """The safety part's reset independence is the reason it is a separate part."""
        adr = ADR_0011.read_text(encoding="utf-8")
        assert "independent reset domain" in adr
        # And the manifest agrees, so neither can change alone.
        assert "independent_reset_domain: true" in manifest_text()

    def test_the_adr_records_why_the_change_happened(self) -> None:
        adr = ADR_0011.read_text(encoding="utf-8")
        assert "eco" in adr.lower()
        assert "CH32V307" in adr


class TestAdr0003IsSupersededNotRewritten:
    def test_the_status_says_superseded(self) -> None:
        text = ADR_0003.read_text(encoding="utf-8")
        assert "superseded by [ADR-0011]" in text

    def test_the_original_decision_text_is_still_there(self) -> None:
        """Superseding means adding a status, not editing history."""
        text = ADR_0003.read_text(encoding="utf-8")
        # The original decision sentence, verbatim.
        assert "The safety MCU targets **RISC-V (rv32imac)**" in text
        assert "CH32V307" in text

    def test_the_superseding_note_points_at_the_new_record(self) -> None:
        text = ADR_0003.read_text(encoding="utf-8")
        assert "ADR-0011-stm32-mcu-selection.md" in text


class TestTheVerificationClaimIsNarrowed:
    def test_the_adr_lists_what_qemu_still_proves(self) -> None:
        adr = ADR_0011.read_text(encoding="utf-8")
        for property_name in ("state machine", "watchdog", "de-dup", "frame codec", "malloc"):
            assert property_name.lower() in adr.lower(), property_name

    def test_the_adr_lists_what_qemu_cannot_prove(self) -> None:
        adr = ADR_0011.read_text(encoding="utf-8")
        for limitation in ("bit timing", "bus-off", "error frames", "electrical"):
            assert limitation.lower() in adr.lower(), limitation

    def test_the_adr_names_the_replacement_path(self) -> None:
        adr = ADR_0011.read_text(encoding="utf-8")
        assert "Renode" in adr
        assert "bench" in adr.lower()

    def test_the_firmware_readme_states_the_narrowed_claim(self) -> None:
        """A green `make qemu` must not read as evidence about FDCAN."""
        text = FIRMWARE_README.read_text(encoding="utf-8")
        assert "does **not** prove anything about FDCAN" in text or "not** prove anything about FDCAN" in text
        assert "Renode" in text

    def test_the_firmware_readme_stops_calling_ch32v307_the_target(self) -> None:
        text = FIRMWARE_README.read_text(encoding="utf-8")
        assert "NOT the board target" in text
        assert "stm32g4" in text
        assert "stm32g0" in text


class TestTheEcoStaysOpen:
    def test_the_eco_is_still_unresolved(self) -> None:
        """The ADR records which parts the board carries; the HAL would close the eco."""
        assert "controller_pcb_mcu_eco_from_legacy_ch32v307" in manifest_text()

    def test_the_adr_says_the_eco_stays_open(self) -> None:
        adr = ADR_0011.read_text(encoding="utf-8")
        assert "unresolved" in adr.lower()

    def test_the_adr_claims_no_physical_evidence(self) -> None:
        """The sentence wraps across lines, so compare on collapsed whitespace."""
        adr = " ".join(ADR_0011.read_text(encoding="utf-8").split())
        assert "NOT_EXECUTED" in adr
        assert "Selecting a part is not building it" in adr


class TestTheRecordIsReachable:
    def test_the_adr_is_in_the_navigation(self) -> None:
        nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
        assert "decisions/ADR-0011-stm32-mcu-selection.md" in nav

    def test_the_adr_has_the_expected_front_matter(self) -> None:
        text = ADR_0011.read_text(encoding="utf-8")
        assert re.search(r"^Status: \*\*accepted\*\*$", text, re.M)
        assert re.search(r"^Supersedes: \[ADR-0003\]", text, re.M)
