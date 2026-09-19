"""Static regression for the Issue #166 household MVP product baseline.

These assertions are deliberately textual. The record is a decision document, so
the failure mode this test guards is not a wrong computation but a silent
deletion: a removed stage separation, a removed target row, a merged mass figure
or a dropped "proposed" status would let the repository claim a household
baseline nobody agreed to.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ADR = ROOT / "docs/decisions/ADR-0007-household-mvp-baseline.md"
CONSTRAINTS = ROOT / "docs/context/CONSTRAINTS.yaml"
CONTEXT_MANIFEST = ROOT / "docs/context/CONTEXT_MANIFEST.md"
PLAN = ROOT / "docs/project-management/plan.md"
DECISION_LOG = ROOT / "docs/project-management/decision-log.md"
PRODUCT_BRIEF = ROOT / "docs/product/product-brief.md"
MKDOCS = ROOT / "mkdocs.yml"

STAGES = ("stage-p0-tabletop", "stage-bench-ur5e", "stage-household-mvp")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _folded(path: Path) -> str:
    return _text(path).casefold()


def test_three_stages_are_named_separately() -> None:
    text = _folded(ADR)
    for stage in STAGES:
        assert stage in text, stage
    # Each stage must be tied to its own authority, not merged into one system.
    assert "adr-0001" in text
    assert "adr-0004" in text
    assert "three stages, three identifiers" in text


def test_household_mvp_scope_is_bounded_to_the_named_issues() -> None:
    text = _folded(ADR)
    for issue in ("/issues/159", "/issues/163", "/issues/164", "/issues/152"):
        assert issue in text, issue
    assert "out of mvp scope" in text
    assert "the one-arm baseline is explicit" in text
    assert "dual-arm coordination is out of scope" in text


def test_target_table_states_value_source_owner_and_status_for_every_required_target() -> None:
    lines = [line for line in _text(ADR).splitlines() if line.startswith("|")]
    header = next(line for line in lines if line.startswith("| Target |"))
    columns = [cell.strip().casefold() for cell in header.strip("|").split("|")]
    assert columns == ["target", "value or gap", "source", "owner", "status"]

    rows = {}
    for line in lines:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) == 5 and cells[0] not in {"Target", "---"}:
            rows[cells[0]] = cells

    required = (
        "Payload",
        "Payload (parcel bay)",
        "Reach",
        "Doorway width",
        "Base footprint",
        "Tip / stability margin",
        "Total mass",
        "Battery / runtime",
        "Compute",
        "Sensor",
        "Safe speed",
    )
    for target in required:
        assert target in rows, f"missing target row: {target}"
        value, source, owner, status = rows[target][1], rows[target][2], rows[target][3], rows[target][4]
        assert value, target
        assert owner, target
        assert status, target
        # Every row must cite a committed repository path or an explicit gap.
        assert source.startswith("`") or source.casefold().startswith("**gap**"), (target, source)

    # Two rows are honest gaps rather than invented numbers.
    assert rows["Doorway width"][1].casefold().startswith("**gap**")
    assert rows["Battery / runtime"][1].casefold().startswith("**gap**")


def test_the_three_historical_masses_are_explained_and_never_merged() -> None:
    text = _folded(ADR)
    for figure in ("55 kg", "6.42 kg", "77.5 kg"):
        assert figure in text, figure
    assert "they are explained, never blended" in text
    # The superseded ledgers are named as the place the old numbers live.
    assert "mass-ledger-legacy.csv" in text
    assert "superseded" in text
    # Exactly one mass may be presented as current.
    assert "the only current total, `rev-d-mass-001`" in text
    assert "no number in this adr is a measured product mass" in text


def test_ur5e_is_bench_hardware_with_no_implicit_arm_swap() -> None:
    text = _folded(ADR)
    assert "ur5e is bench hardware" in text
    assert "no measured mobile payload" in text
    assert "no measured mobile stability evidence" in text
    assert "no implicit arm swap" in text
    # ADR-0004 stays accepted; this record must not rewrite it.
    assert "not rewritten" in text or "not** rewritten" in text


def test_resource_budget_hooks_require_target_measurements_with_ollama_disabled() -> None:
    text = _folded(ADR)
    assert "/issues/79" in text
    for needed in ("ros 2", "nav2", "moveit", "perception"):
        assert needed in text, needed
    assert "disabled by default" in text
    assert "model_policy.yaml" in text
    assert "not_executed" in text
    # A development-host number must not be reported as a product budget.
    assert "development-host number must not be reported" in text


def test_later_phases_are_named_and_appliance_interaction_is_phase_2() -> None:
    text = _folded(ADR)
    assert "phase 2" in text
    assert "/issues/160" in text
    for later in ("elevators", "stairs", "public lockers", "outdoor travel"):
        assert later in text, later


def test_approval_register_fails_closed_until_each_owner_approves() -> None:
    text = _folded(ADR)
    assert "approval register" in text
    assert "explicit blocking objection" in text
    assert "a missing row is not approval" in text
    for role in ("product", "motion", "integration", "safety", "hardware"):
        assert f"| {role} |" in text, role
    assert text.count("`required`") >= 5
    assert "status: **proposed**" in text


def test_context_constraints_carry_the_stage_boundary() -> None:
    constraints = yaml.safe_load(_text(CONSTRAINTS))
    project = constraints["project"]
    assert project["p0_scope"] == "fixed-tabletop-single-arm-simulator"
    assert project["stages"] == {
        "p0_tabletop_simulator": "STAGE-P0-TABLETOP",
        "fixed_development_bench": "STAGE-BENCH-UR5E",
        "household_product": "STAGE-HOUSEHOLD-MVP",
    }
    mvp = project["household_mvp"]
    assert mvp["arm_count"] == 1
    assert mvp["rgbd_camera_count"] == 1
    assert mvp["onboard_compute_count"] == 1
    assert mvp["safety_mcu_count"] == 1
    assert mvp["dual_arm_coordination"] == "out_of_scope"
    assert mvp["bounded_scenarios"] == [159, 163, 164, 152]
    assert mvp["status"] == "proposed_pending_owner_approval"


def test_the_record_is_published_and_indexed() -> None:
    assert "decisions/ADR-0007-household-mvp-baseline.md" in _text(MKDOCS)
    assert "ADR-0007" in _text(DECISION_LOG)
    assert "ADR-0007-household-mvp-baseline.md" in _text(PRODUCT_BRIEF)
    assert "ADR-0007-household-mvp-baseline.md" in _text(PLAN)
    # Accepted earlier ADRs are indexed unchanged.
    log = _text(DECISION_LOG)
    assert "ADR-0001 | existing | keep P0 scope evidence-first and bounded | accepted" in log
    assert "ADR-0004 | existing | use UR5e plus Robotiq baseline | accepted" in log
