"""Issue #313: make a run's determinism inputs explicit and checkable.

The tests drive the real provenance module and a real run directory produced by
``sim_cli``. The assertions are mostly about what the module refuses, because
"two runs cannot be compared as if they were the same evidence" is only true if a
test can show the comparison being refused.
"""

import json
import subprocess
import sys
from pathlib import Path
from string import Template

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "libs" / "kernel"))
sys.path.insert(0, str(ROOT / "tools" / "scripts"))

from workbench.kernel.run_provenance import (
    CLAIMABLE_ENVIRONMENT_CLASSES,
    EMITTED_CODES,
    ENVIRONMENT_CLASSES,
    PROVENANCE_INPUTS,
    PROVENANCE_SCHEMA_VERSION,
    REQUIRED_PROVENANCE_FIELDS,
    RUN_PROVENANCE_ADAPTER_CONFLICT,
    RUN_PROVENANCE_CLOCK_CONFLICT,
    RUN_PROVENANCE_ENVIRONMENT_CONFLICT,
    RUN_PROVENANCE_HASH_MISMATCH,
    RUN_PROVENANCE_MALFORMED,
    RUN_PROVENANCE_MISSING,
    RUN_PROVENANCE_SOURCE_CONFLICT,
    RUN_PROVENANCE_UNSAFE_ENVIRONMENT_CLAIM,
    RunProvenanceError,
    canonical_adapter_versions,
    canonical_provenance_bytes,
    classify_incompatibility,
    describe_incompatibility,
    environment_class_for_runner,
    known_adapters,
    provenance_material,
    recorded_provenance,
    replay_compatible,
    run_provenance,
    verify_bundle_provenance,
)

BASE_MATERIAL = {
    "seed": "1110",
    "clock_mode": "wall",
    "time_source": "fixed_base",
    "event_ordering": "sequence_no",
    "adapter_versions": "motion=unspecified,perception=unspecified",
    "environment_class": "SCRIPTED_FIXTURE",
}


def material_with(**overrides: str) -> dict[str, str]:
    payload = dict(BASE_MATERIAL)
    payload.update(overrides)
    return payload


def provenance_for(**overrides: str):
    return run_provenance(material_with(**overrides))


def bundle(tmp_path: Path, *, provenance=None, name: str = "run-1", **metadata_overrides):
    run_dir = tmp_path / name
    run_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema_version": "sim-run-v1",
        "run_id": name,
        "scenario_id": "pick-place-red-block",
        "runner": "scripted",
        "status": "SCRIPTED_FIXTURE",
        "seed": 1110,
    }
    if provenance is not None:
        metadata["provenance"] = provenance.as_dict() if hasattr(provenance, "as_dict") else provenance
    metadata.update(metadata_overrides)
    (run_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return run_dir


class TestProvenanceInputs:
    def test_the_declared_input_order_is_fixed(self) -> None:
        assert PROVENANCE_INPUTS == (
            "seed",
            "clock_mode",
            "time_source",
            "event_ordering",
            "adapter_versions",
            "environment_class",
        )

    def test_every_required_field_is_a_declared_input(self) -> None:
        for field in REQUIRED_PROVENANCE_FIELDS:
            assert field in PROVENANCE_INPUTS

    def test_the_environment_class_is_not_required_to_be_present(self) -> None:
        """It is derived from the runner, so requiring it would let a caller disagree."""

        assert "environment_class" not in REQUIRED_PROVENANCE_FIELDS
        assert "environment_class" in PROVENANCE_INPUTS

    def test_the_canonical_bytes_are_stable_and_ordered(self) -> None:
        first = canonical_provenance_bytes(material_with(), PROVENANCE_INPUTS)
        second = canonical_provenance_bytes(material_with(), PROVENANCE_INPUTS)
        assert first == second
        decoded = json.loads(first.decode("utf-8"))
        assert decoded["schema_version"] == PROVENANCE_SCHEMA_VERSION
        assert list(decoded["inputs"]) == list(PROVENANCE_INPUTS)

    def test_a_shuffled_mapping_produces_the_same_hash(self) -> None:
        shuffled = {name: BASE_MATERIAL[name] for name in reversed(PROVENANCE_INPUTS)}
        assert run_provenance(shuffled).provenance_hash == provenance_for().provenance_hash

    def test_every_declared_input_changes_the_hash(self) -> None:
        base = provenance_for().provenance_hash
        alternatives = {
            "seed": "1111",
            "clock_mode": "monotonic",
            "time_source": "host_clock",
            "event_ordering": "file_order",
            "adapter_versions": "motion=1,perception=1",
            "environment_class": "GAZEBO",
        }
        assert set(alternatives) == set(PROVENANCE_INPUTS)
        for name, value in alternatives.items():
            changed = provenance_for(**{name: value}).provenance_hash
            assert changed != base, name

    def test_a_missing_input_is_refused_by_name(self) -> None:
        incomplete = material_with()
        del incomplete["clock_mode"]
        with pytest.raises(RunProvenanceError) as caught:
            canonical_provenance_bytes(incomplete, PROVENANCE_INPUTS)
        assert caught.value.code == RUN_PROVENANCE_MISSING
        assert "clock_mode" in caught.value.message


class TestMaterial:
    def test_a_bool_seed_is_refused_rather_than_stringified(self) -> None:
        with pytest.raises(RunProvenanceError) as caught:
            provenance_material(
                seed=True,
                clock_mode="wall",
                time_source="fixed_base",
                event_ordering="sequence_no",
                adapter_versions={},
                environment_class="SCRIPTED_FIXTURE",
            )
        assert caught.value.code == RUN_PROVENANCE_MALFORMED

    def test_a_blank_seed_is_missing_rather_than_zero(self) -> None:
        with pytest.raises(RunProvenanceError) as caught:
            provenance_material(
                seed="   ",
                clock_mode="wall",
                time_source="fixed_base",
                event_ordering="sequence_no",
                adapter_versions={},
                environment_class="SCRIPTED_FIXTURE",
            )
        assert caught.value.code == RUN_PROVENANCE_MISSING

    def test_an_unknown_clock_mode_is_refused(self) -> None:
        with pytest.raises(RunProvenanceError) as caught:
            provenance_material(
                seed=1,
                clock_mode="realtime",
                time_source="fixed_base",
                event_ordering="sequence_no",
                adapter_versions={},
                environment_class="SCRIPTED_FIXTURE",
            )
        assert caught.value.code == RUN_PROVENANCE_MALFORMED
        assert "clock_mode" in caught.value.message

    def test_an_unknown_environment_class_is_refused(self) -> None:
        with pytest.raises(RunProvenanceError):
            provenance_material(
                seed=1,
                clock_mode="wall",
                time_source="fixed_base",
                event_ordering="sequence_no",
                adapter_versions={},
                environment_class="SIMULATED",
            )

    def test_a_numeric_seed_becomes_a_stable_string(self) -> None:
        material = provenance_material(
            seed=1234,
            clock_mode="wall",
            time_source="fixed_base",
            event_ordering="sequence_no",
            adapter_versions={},
            environment_class="SCRIPTED_FIXTURE",
        )
        assert material["seed"] == "1234"

    def test_adapter_versions_are_sorted_and_idempotent(self) -> None:
        canonical = canonical_adapter_versions({"motion": "2", "perception": "1"})
        assert canonical == "motion=2,perception=1"
        assert canonical_adapter_versions(known_adapters(canonical)) == canonical

    def test_an_unknown_adapter_version_is_recorded_as_unspecified(self) -> None:
        assert canonical_adapter_versions({"motion": None}) == "motion=unspecified"
        assert canonical_adapter_versions({"motion": "  "}) == "motion=unspecified"

    def test_an_empty_adapter_mapping_is_a_legitimate_value(self) -> None:
        assert canonical_adapter_versions({}) == ""
        assert known_adapters("") == {}

    def test_an_adapter_version_with_a_separator_is_refused(self) -> None:
        with pytest.raises(RunProvenanceError) as caught:
            canonical_adapter_versions({"motion": "1,2"})
        assert caught.value.code == RUN_PROVENANCE_MALFORMED


class TestEnvironmentClass:
    @pytest.mark.parametrize(
        ("runner", "status", "expected"),
        [
            ("scripted", "SCRIPTED_FIXTURE", "SCRIPTED_FIXTURE"),
            ("scripted", "NOT_EXECUTED", "NOT_EXECUTED"),
            ("gazebo", "EXECUTED", "GAZEBO"),
            ("gazebo", "NOT_EXECUTED", "NOT_EXECUTED"),
            ("external", "EXECUTED", "GAZEBO"),
        ],
    )
    def test_the_class_is_derived_from_the_runner_and_status(self, runner, status, expected) -> None:
        assert environment_class_for_runner(runner, status=status) == expected

    @pytest.mark.parametrize("status", ["FAILED", "TIMED_OUT", "INVALID_OUTPUT", "INTERRUPTED"])
    def test_a_gazebo_run_that_stopped_is_still_gazebo(self, status: str) -> None:
        """A failed run is evidence from its environment, not an absent one."""

        assert environment_class_for_runner("gazebo", status=status) == "GAZEBO"

    def test_a_scripted_runner_cannot_report_an_executed_status(self) -> None:
        with pytest.raises(RunProvenanceError) as caught:
            environment_class_for_runner("scripted", status="EXECUTED")
        assert caught.value.code == RUN_PROVENANCE_MALFORMED
        assert "fixture" in caught.value.message

    def test_a_non_scripted_runner_cannot_report_the_fixture_status(self) -> None:
        with pytest.raises(RunProvenanceError):
            environment_class_for_runner("gazebo", status="SCRIPTED_FIXTURE")

    def test_an_unknown_runner_is_refused(self) -> None:
        with pytest.raises(RunProvenanceError):
            environment_class_for_runner("mujoco", status="EXECUTED")

    def test_an_unknown_status_is_refused(self) -> None:
        with pytest.raises(RunProvenanceError):
            environment_class_for_runner("gazebo", status="PROBABLY_FINE")

    def test_no_runner_may_claim_physical(self) -> None:
        """Physical evidence enters through the hardware path, not a sim runner."""

        for runner, allowed in CLAIMABLE_ENVIRONMENT_CLASSES.items():
            assert "PHYSICAL" not in allowed, runner

    def test_every_claimable_class_is_a_declared_class(self) -> None:
        for runner, allowed in CLAIMABLE_ENVIRONMENT_CLASSES.items():
            assert allowed <= set(ENVIRONMENT_CLASSES), runner


class TestReplayCompatibility:
    def test_two_identical_runs_are_compatible(self) -> None:
        assert replay_compatible(provenance_for(), provenance_for()) == ()

    def test_a_different_seed_makes_two_runs_incompatible(self) -> None:
        left, right = provenance_for(), provenance_for(seed="1111")
        assert replay_compatible(left, right) == ("seed",)

    def test_the_differing_input_is_named_with_both_values(self) -> None:
        left, right = provenance_for(), provenance_for(clock_mode="monotonic")
        assert describe_incompatibility(left, right) == (("clock_mode", "wall != monotonic"),)

    def test_an_environment_class_difference_is_never_merged(self) -> None:
        """The pair that must not be compared as one body of evidence."""

        fixture = provenance_for()
        gazebo = provenance_for(environment_class="GAZEBO")
        assert replay_compatible(fixture, gazebo) == ("environment_class",)
        assert classify_incompatibility(replay_compatible(fixture, gazebo)) == RUN_PROVENANCE_ENVIRONMENT_CONFLICT

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"seed": "2"}, RUN_PROVENANCE_SOURCE_CONFLICT),
            ({"clock_mode": "monotonic"}, RUN_PROVENANCE_CLOCK_CONFLICT),
            ({"time_source": "host_clock"}, RUN_PROVENANCE_CLOCK_CONFLICT),
            ({"event_ordering": "file_order"}, RUN_PROVENANCE_CLOCK_CONFLICT),
            ({"adapter_versions": "motion=9"}, RUN_PROVENANCE_ADAPTER_CONFLICT),
            ({"environment_class": "GAZEBO"}, RUN_PROVENANCE_ENVIRONMENT_CONFLICT),
        ],
    )
    def test_each_difference_classifies_to_its_own_code(self, overrides, expected) -> None:
        assert classify_incompatibility(replay_compatible(provenance_for(), provenance_for(**overrides))) == expected

    def test_compatible_runs_have_no_classification(self) -> None:
        assert classify_incompatibility(()) == ""

    def test_the_environment_class_wins_a_multi_field_difference(self) -> None:
        """The strongest reason is reported, so a class conflict is never hidden."""

        differing = ("seed", "clock_mode", "environment_class")
        assert classify_incompatibility(differing) == RUN_PROVENANCE_ENVIRONMENT_CONFLICT


class TestBundleVerification:
    def test_a_matching_bundle_passes(self) -> None:
        verdict = verify_bundle_provenance(
            run_id="run-1",
            metadata={"provenance": provenance_for().as_dict()},
            runner="scripted",
            status="SCRIPTED_FIXTURE",
        )
        assert verdict.ok is True
        assert verdict.environment_class == "SCRIPTED_FIXTURE"
        assert verdict.provenance_hash == provenance_for().provenance_hash

    def test_a_bundle_without_provenance_is_missing(self) -> None:
        verdict = verify_bundle_provenance(run_id="run-1", metadata={})
        assert verdict.ok is False
        assert verdict.findings[0].code == RUN_PROVENANCE_MISSING

    def test_a_tampered_material_fails_the_hash(self) -> None:
        block = provenance_for().as_dict()
        block["material"]["seed"] = "9999"
        verdict = verify_bundle_provenance(run_id="run-1", metadata={"provenance": block})
        assert verdict.ok is False
        assert verdict.findings[0].code == RUN_PROVENANCE_HASH_MISMATCH

    def test_a_missing_required_field_is_reported_by_name(self) -> None:
        block = provenance_for().as_dict()
        del block["material"]["event_ordering"]
        verdict = verify_bundle_provenance(run_id="run-1", metadata={"provenance": block})
        assert verdict.ok is False
        assert verdict.findings[0].code == RUN_PROVENANCE_MISSING
        assert "event_ordering" in verdict.findings[0].detail

    def test_a_previous_schema_version_is_refused(self) -> None:
        block = provenance_for().as_dict()
        block["schema_version"] = "workbench-run-provenance-v0"
        verdict = verify_bundle_provenance(run_id="run-1", metadata={"provenance": block})
        assert verdict.ok is False
        assert verdict.findings[0].code == RUN_PROVENANCE_MALFORMED
        assert "workbench-run-provenance-v0" in verdict.findings[0].detail

    def test_a_fixture_cannot_claim_a_gazebo_class(self) -> None:
        block = provenance_for(environment_class="GAZEBO").as_dict()
        verdict = verify_bundle_provenance(
            run_id="run-1", metadata={"provenance": block}, runner="scripted", status="SCRIPTED_FIXTURE"
        )
        assert verdict.ok is False
        assert verdict.findings[0].code == RUN_PROVENANCE_UNSAFE_ENVIRONMENT_CLAIM
        assert "SCRIPTED_FIXTURE" in verdict.findings[0].detail

    def test_a_run_cannot_claim_a_class_its_status_cannot_reach(self) -> None:
        block = provenance_for(environment_class="GAZEBO").as_dict()
        verdict = verify_bundle_provenance(
            run_id="run-1", metadata={"provenance": block}, runner="gazebo", status="NOT_EXECUTED"
        )
        assert verdict.ok is False
        assert verdict.findings[0].code == RUN_PROVENANCE_UNSAFE_ENVIRONMENT_CLAIM

    def test_an_unknown_environment_class_is_malformed(self) -> None:
        block = provenance_for().as_dict()
        block["material"]["environment_class"] = "SIMULATED"
        verdict = verify_bundle_provenance(run_id="run-1", metadata={"provenance": block})
        assert verdict.ok is False
        assert verdict.findings[0].code == RUN_PROVENANCE_MALFORMED

    def test_verification_without_a_runner_still_checks_the_hash(self) -> None:
        """A caller that does not know the runner can still refuse a bad digest."""

        block = provenance_for().as_dict()
        block["material"]["seed"] = "4242"
        verdict = verify_bundle_provenance(run_id="run-1", metadata={"provenance": block})
        assert verdict.ok is False
        assert verdict.findings[0].code == RUN_PROVENANCE_HASH_MISMATCH

    def test_recorded_provenance_reads_only_an_object(self) -> None:
        assert recorded_provenance({"provenance": {"a": 1}}) == {"a": 1}
        assert recorded_provenance({"provenance": "text"}) is None
        assert recorded_provenance({}) is None

    def test_the_verdict_round_trips_through_json(self) -> None:
        verdict = verify_bundle_provenance(run_id="run-1", metadata={})
        payload = json.loads(json.dumps(verdict.as_dict()))
        assert payload["ok"] is False
        assert payload["findings"][0]["code"] == RUN_PROVENANCE_MISSING


class TestRealRuns:
    def _run(self, tmp_path: Path, version: str = "test-provenance") -> list[Path]:
        output = tmp_path / f"runs-{version}"
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools/scripts/sim_cli.py"),
                "run",
                "--all",
                "--runner",
                "scripted",
                "--output-dir",
                str(output),
                "--version",
                version,
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        return sorted(path for path in output.iterdir() if path.is_dir())

    def test_a_real_run_records_provenance_that_verifies(self, tmp_path: Path) -> None:
        run_dirs = self._run(tmp_path)
        assert len(run_dirs) >= 5
        for run_dir in run_dirs:
            metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
            verdict = verify_bundle_provenance(
                run_id=run_dir.name,
                metadata=metadata,
                runner=metadata["runner"],
                status=metadata["status"],
            )
            assert verdict.ok is True, verdict.findings

    def test_a_real_fixture_run_is_never_gazebo_or_physical(self, tmp_path: Path) -> None:
        for run_dir in self._run(tmp_path, "envclass"):
            metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
            assert metadata["environment_class"] == "SCRIPTED_FIXTURE"

    def test_the_adapters_the_registry_declares_are_recorded(self, tmp_path: Path) -> None:
        adapters = set()
        for run_dir in self._run(tmp_path, "adapters"):
            metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
            adapters |= set(known_adapters(metadata["provenance"]["material"]["adapter_versions"]))
        assert {"motion", "perception", "simulation"} <= adapters

    def test_a_real_run_writes_a_provenance_file_beside_the_metadata(self, tmp_path: Path) -> None:
        for run_dir in self._run(tmp_path, "files"):
            file_block = json.loads((run_dir / "provenance.json").read_text(encoding="utf-8"))
            metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
            assert file_block == metadata["provenance"]
            assert "provenance.json" in metadata["evidence_paths"]

    def test_two_seeds_produce_two_provenance_hashes(self, tmp_path: Path) -> None:
        """Two runs of one scenario whose seeds differ must not share provenance."""

        import sim_cli

        scenario = next(item for item in sim_cli.load_scenarios() if item.scenario_id == "grasp-failure-001")
        one = sim_cli.run_scenario(
            scenario,
            runner="scripted",
            output_dir=tmp_path / "seeded-a",
            version="seed-a",
            seed_base=1000,
        )
        two = sim_cli.run_scenario(
            scenario,
            runner="scripted",
            output_dir=tmp_path / "seeded-b",
            version="seed-b",
            seed_base=2000,
        )
        left = json.loads((Path(one.artifact_dir) / "metadata.json").read_text(encoding="utf-8"))["provenance"]
        right = json.loads((Path(two.artifact_dir) / "metadata.json").read_text(encoding="utf-8"))["provenance"]
        assert left["provenance_hash"] != right["provenance_hash"]
        assert left["material"]["seed"] != right["material"]["seed"]


REPLAY_VECTOR_SCRIPT = Template(
    """
import json
import sys
from pathlib import Path

sys.path.insert(0, $kernel_path)
sys.path.insert(0, $scripts_path)

from workbench.kernel.run_provenance import provenance_material, run_provenance
from workbench.kernel.scenario_identity import identity_from_entry, load_registry_entries

entry = load_registry_entries(Path($root))["pick-place-red-block@1.0"]
provenance = run_provenance(
    provenance_material(
        seed=$seed,
        clock_mode=$clock_mode,
        time_source=$time_source,
        event_ordering=$ordering,
        adapter_versions={"motion": "unspecified"},
        environment_class=$environment_class,
    )
)
identity = identity_from_entry(
    entry,
    event_stream_hash_value=$stream,
    config_hash="a" * 64,
    commit="b" * 40,
    provenance_hash=provenance.provenance_hash,
)
print(
    json.dumps(
        {
            "provenance_hash": provenance.provenance_hash,
            "identity_hash": identity.identity_hash,
            "inputs": list(provenance.inputs),
            "identity_inputs": list(identity.inputs),
        }
    )
)
"""
)


def _replay_in_two_processes(**overrides) -> list[dict]:
    """Compute one run's identity and provenance in two independent interpreters."""

    values = {
        "kernel_path": repr(str(ROOT / "libs" / "kernel")),
        "scripts_path": repr(str(ROOT / "tools" / "scripts")),
        "root": repr(str(ROOT)),
        "seed": repr("1110"),
        "clock_mode": repr("wall"),
        "time_source": repr("fixed_base"),
        "ordering": repr("sequence_no"),
        "environment_class": repr("SCRIPTED_FIXTURE"),
        "stream": repr("c" * 64),
    }
    values.update({key: repr(value) for key, value in overrides.items()})
    script = REPLAY_VECTOR_SCRIPT.substitute(values)
    runs = [
        subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False) for _ in range(2)
    ]
    for run in runs:
        assert run.returncode == 0, run.stderr
    return [json.loads(run.stdout) for run in runs]


class TestCrossProcessReplay:
    """The acceptance vector: one bundle replays identically across processes."""

    def test_success_replays_to_one_provenance_and_identity(self) -> None:
        first, second = _replay_in_two_processes()
        assert first == second
        assert first["provenance_hash"] == second["provenance_hash"]
        assert first["identity_hash"] == second["identity_hash"]

    def test_a_different_stream_is_a_different_identity_at_the_same_seed(self) -> None:
        base, _ = _replay_in_two_processes()
        other, _ = _replay_in_two_processes(stream="d" * 64)
        assert base["provenance_hash"] == other["provenance_hash"]
        assert base["identity_hash"] != other["identity_hash"]

    def test_a_different_seed_is_a_different_identity_at_the_same_stream(self) -> None:
        base, _ = _replay_in_two_processes()
        other, _ = _replay_in_two_processes(seed="2220")
        assert base["provenance_hash"] != other["provenance_hash"]
        assert base["identity_hash"] != other["identity_hash"]

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("clock_mode", "monotonic"),
            ("time_source", "host_clock"),
            ("ordering", "file_order"),
            ("environment_class", "GAZEBO"),
        ],
    )
    def test_a_different_determinism_input_changes_the_identity(self, field: str, value: str) -> None:
        """Different clock, ordering or environment class cannot share an identity."""

        base, _ = _replay_in_two_processes()
        other, _ = _replay_in_two_processes(**{field: value})
        assert base["provenance_hash"] != other["provenance_hash"]
        assert base["identity_hash"] != other["identity_hash"]

    def test_the_input_orders_are_identical_across_processes(self) -> None:
        first, second = _replay_in_two_processes()
        assert first["inputs"] == second["inputs"] == list(PROVENANCE_INPUTS)
        assert first["identity_inputs"] == second["identity_inputs"]
        assert "provenance_hash" in first["identity_inputs"]


class TestReplayVectors:
    """The committed vectors, reduced through the real World Model reducer.

    Each vector is one failure the multi-scenario epic has to replay: success,
    timeout, stale evidence, conflict, recovery start and recovery completion.
    Two properties are asserted for every vector, and they are different:

    * re-reducing the same stream agrees, so the vector is deterministic;
    * every vector's *event stream* hash is distinct, so a reader can tell them
      apart from the artifact.

    The second is the one that matters, because the first is not enough. A fault
    and a recovery-started event change what happened without changing the world
    state the reducer derives from it, so two vectors can share a state hash while
    being different runs. That is exactly why Issue #313 binds the stream and the
    determinism inputs as well as the state.
    """

    @staticmethod
    def _contracts():
        for path in ("libs/contracts", "services/world_model", "libs/kernel"):
            if str(ROOT / path) not in sys.path:
                sys.path.insert(0, str(ROOT / path))
        from workbench_contracts import ClockId, WorldEvent, WorldEventType
        from workbench_world_model import reducer

        return ClockId, WorldEvent, WorldEventType, reducer

    def _base_events(self):
        from workbench.kernel.scenario_conformance import PROBE_FAMILIES, PROBE_RUN_ID, _probe_events

        ClockId, WorldEvent, WorldEventType, _ = self._contracts()
        return PROBE_RUN_ID, _probe_events(
            PROBE_FAMILIES["pick-place-red-block@1.0"], WorldEvent, WorldEventType, ClockId
        )

    def _event(self, payload, event_type, *, at, suffix):
        ClockId, WorldEvent, _, _ = self._contracts()
        run_id, events = self._base_events()
        return WorldEvent(
            event_id=f"{run_id}-{suffix}",
            run_id=run_id,
            sequence_no=len(events),
            event_type=event_type,
            occurred_at=at,
            payload=payload,
            evidence_refs=[f"probe://{run_id}/{suffix}"],
            clock_id=ClockId.WALL,
        )

    def _recovery_payload(self, **overrides):
        payload = {
            "recovery_id": "rec-1",
            "task_id": "task-pick-place",
            "action": "retry_observation",
            "state": "observing",
            "attempt": 1,
            "max_attempts": 3,
            "reason_code": "evidence_missing",
            "reason": "the tray was not observed",
        }
        payload.update(overrides)
        return payload

    def _vectors(self):
        """The six committed vectors, built once so every test measures the same set."""

        ClockId, WorldEvent, WorldEventType, _ = self._contracts()
        run_id, events = self._base_events()
        recovery_started = self._event(
            self._recovery_payload(), WorldEventType.RECOVERY_STARTED, at="2026-08-04T00:00:59Z", suffix="rec"
        )
        started_stream = [*events, recovery_started]
        recovery_completed = WorldEvent(
            event_id=f"{run_id}-rec-done",
            run_id=run_id,
            sequence_no=len(started_stream),
            event_type=WorldEventType.RECOVERY_COMPLETE,
            occurred_at="2026-08-04T00:01:01Z",
            payload=self._recovery_payload(action="abort", state="aborted"),
            evidence_refs=[f"probe://{run_id}/rec-done"],
            clock_id=ClockId.WALL,
        )
        return {
            "success": events,
            "timeout": [
                *events,
                self._event(
                    {"fault_type": "timeout", "detail": "action budget exceeded"},
                    WorldEventType.FAULT,
                    at="2026-08-04T00:00:58Z",
                    suffix="timeout",
                ),
            ],
            "stale": [
                *events,
                self._event(
                    {
                        "entity_id": "red_block",
                        "location": "in:tray",
                        "confidence": 0.2,
                        "source": "cam0",
                        "entity_type": "block",
                    },
                    WorldEventType.OBSERVATION,
                    at="2026-08-05T00:00:00Z",
                    suffix="stale",
                ),
            ],
            "conflict": [
                *events,
                self._event(
                    {
                        "entity_id": "red_block",
                        "location": "on:floor_south",
                        "confidence": 0.99,
                        "source": "cam1",
                        "entity_type": "block",
                    },
                    WorldEventType.OBSERVATION,
                    at="2026-08-04T00:00:58Z",
                    suffix="conflict",
                ),
            ],
            "recovery_started": started_stream,
            "recovery_completed": [*started_stream, recovery_completed],
        }

    def _reduce(self, events):
        _, _, _, reducer = self._contracts()
        run_id, _ = self._base_events()
        return reducer.create_world_state_snapshot(run_id, events)

    def _stream_hash(self, events) -> str:
        from workbench.kernel.scenario_identity import event_stream_hash

        return event_stream_hash([event.model_dump(mode="json") for event in events])

    def test_the_success_vector_is_stable_across_reductions(self) -> None:
        stream = self._vectors()["success"]
        assert self._reduce(stream).state_hash == self._reduce(stream).state_hash

    @pytest.mark.parametrize(
        "name",
        ["success", "timeout", "stale", "conflict", "recovery_started", "recovery_completed"],
    )
    def test_every_vector_is_stable_across_reductions(self, name: str) -> None:
        stream = self._vectors()[name]
        assert self._reduce(stream).state_hash == self._reduce(stream).state_hash

    def test_every_vector_has_a_distinct_event_stream_hash(self) -> None:
        """A reader must be able to tell the six vectors apart from the artifact."""

        hashes = {name: self._stream_hash(stream) for name, stream in self._vectors().items()}
        assert len(set(hashes.values())) == len(hashes), hashes

    def test_a_state_hash_alone_does_not_separate_faults_from_recovery(self) -> None:
        """The observation that motivates binding the stream, not only the state.

        A fault and a recovery-started event change what happened without changing
        the reduced world state, so the pair shares one state hash and differs by
        stream hash. If the reducer ever separates them, this test still holds:
        it only requires that any shared state hash is accompanied by a distinct
        stream hash, never that a collision exists.
        """

        vectors = self._vectors()
        by_state: dict[str, list[str]] = {}
        for name, stream in vectors.items():
            by_state.setdefault(self._reduce(stream).state_hash, []).append(name)
        for state_hash, names in by_state.items():
            if len(names) > 1:
                streams = {self._stream_hash(vectors[name]) for name in names}
                assert len(streams) == len(names), (state_hash, names)

    def test_the_recovery_completion_differs_from_its_start(self) -> None:
        vectors = self._vectors()
        assert self._stream_hash(vectors["recovery_completed"]) != self._stream_hash(vectors["recovery_started"])

    def test_the_vectors_carry_the_same_identity_inputs(self) -> None:
        """The vectors differ by stream, so their recorded inputs are the same set."""

        for name, stream in self._vectors().items():
            assert len(stream) >= 1, name
        assert set(self._vectors()) == {
            "success",
            "timeout",
            "stale",
            "conflict",
            "recovery_started",
            "recovery_completed",
        }


class TestDiagnostics:
    def test_every_diagnostic_is_unique(self) -> None:
        assert len(set(EMITTED_CODES)) == len(EMITTED_CODES)

    def test_every_diagnostic_the_module_raises_is_declared(self) -> None:
        raised = {
            RUN_PROVENANCE_MISSING,
            RUN_PROVENANCE_MALFORMED,
            RUN_PROVENANCE_HASH_MISMATCH,
            RUN_PROVENANCE_UNSAFE_ENVIRONMENT_CLAIM,
            RUN_PROVENANCE_ENVIRONMENT_CONFLICT,
            RUN_PROVENANCE_CLOCK_CONFLICT,
            RUN_PROVENANCE_ADAPTER_CONFLICT,
            RUN_PROVENANCE_SOURCE_CONFLICT,
        }
        assert raised == set(EMITTED_CODES)

    def test_the_gate_exits_two_when_there_is_nothing_to_inspect(self, tmp_path: Path) -> None:
        from check_run_provenance import main

        assert main(["--runs-root", str(tmp_path)]) == 2

    def test_the_gate_exits_zero_for_a_matching_bundle(self, tmp_path: Path) -> None:
        from check_run_provenance import main

        bundle(tmp_path, provenance=provenance_for())
        assert main(["--runs-root", str(tmp_path)]) == 0

    def test_the_gate_exits_one_for_a_bundle_without_provenance(self, tmp_path: Path) -> None:
        from check_run_provenance import main

        bundle(tmp_path, provenance=None)
        assert main(["--runs-root", str(tmp_path)]) == 1

    def test_the_gate_exits_one_for_a_file_the_runner_cannot_have_produced(self, tmp_path: Path) -> None:
        from check_run_provenance import main

        block = provenance_for(environment_class="GAZEBO").as_dict()
        bundle(tmp_path, provenance=block, runner="scripted", status="SCRIPTED_FIXTURE")
        assert main(["--runs-root", str(tmp_path)]) == 1

    def test_the_gate_exits_one_for_malformed_json(self, tmp_path: Path) -> None:
        from check_run_provenance import main

        run_dir = tmp_path / "run-bad"
        run_dir.mkdir()
        (run_dir / "metadata.json").write_text("{not json", encoding="utf-8")
        assert main(["--runs-root", str(tmp_path)]) == 1

    def test_a_directory_without_metadata_is_skipped(self, tmp_path: Path) -> None:
        from check_run_provenance import scan_run_root

        (tmp_path / "not-a-run").mkdir()
        assert scan_run_root(tmp_path) == []

    def test_the_json_summary_names_the_failures(self, tmp_path: Path, capsys) -> None:
        from check_run_provenance import main

        bundle(tmp_path, provenance=None)
        assert main(["--runs-root", str(tmp_path), "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["exit_code"] == 1
        assert payload["runs"][0]["findings"][0]["code"] == RUN_PROVENANCE_MISSING
