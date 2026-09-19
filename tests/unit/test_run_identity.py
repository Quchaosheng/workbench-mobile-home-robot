"""Issue #302: bind run artifacts, replay and verification to scenario identity.

The tests drive the real identity module, the real registry reader and a real
run directory produced by ``sim_cli``. The assertions are about what the gate
refuses, because "a bundle cannot be replayed against the wrong definition" is
only true if a test can show it refusing.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "libs" / "kernel"))
sys.path.insert(0, str(ROOT / "tools" / "scripts"))

from workbench.kernel.scenario_identity import (
    EMITTED_CODES,
    HASH_ALGORITHM,
    IDENTITY_INPUTS,
    REQUIRED_IDENTITY_FIELDS,
    RUN_IDENTITY_EVENT_STREAM_MISMATCH,
    RUN_IDENTITY_EVENTS_UNREADABLE,
    RUN_IDENTITY_MALFORMED,
    RUN_IDENTITY_MISMATCH,
    RUN_IDENTITY_MISSING,
    RUN_IDENTITY_UNKNOWN_SCENARIO,
    RUN_IDENTITY_UNSAFE_RELEASE_CLAIM,
    RunIdentityError,
    canonical_identity_bytes,
    event_stream_hash,
    hash_events_file,
    identity_from_entry,
    load_registry_entries,
    policy_version,
    recorded_identity,
    run_identity,
    scan_run_root,
    verifier_rule_version,
)

REGISTRY = load_registry_entries(ROOT)

BASE_MATERIAL = {
    "scenario_id": "pick-place-red-block",
    "scenario_version": "1.0",
    "evidence_policy_version": "sha256:aaaaaaaaaaaaaaaa",
    "verifier_rule_version": "sha256:bbbbbbbbbbbbbbbb",
    "event_stream_hash": "c" * 64,
    "world_version": "WorkbenchSim-v0",
    "config_hash": "d" * 64,
    "commit": "e" * 40,
}


def material_with(**overrides: str) -> dict[str, str]:
    payload = dict(BASE_MATERIAL)
    payload.update(overrides)
    return payload


def identity_for(scenario_id: str = "pick-place-red-block", *, version: str = "1.0"):
    entry = REGISTRY[f"{scenario_id}@{version}"]
    return identity_from_entry(
        entry,
        event_stream_hash_value="f" * 64,
        config_hash="a" * 64,
        commit="b" * 40,
    )


def bundle(tmp_path: Path, *, identity=None, name: str = "run-1", **metadata_overrides):
    run_dir = tmp_path / name
    run_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema_version": "sim-run-v1",
        "run_id": name,
        "scenario_id": "pick-place-red-block",
        "scenario_version": "1.0",
        "registry_identity": "pick-place-red-block@1.0",
        "release_eligible": False,
        "evidence_status": "SCRIPTED_FIXTURE",
    }
    if identity is not None:
        metadata["identity"] = identity.as_dict() if hasattr(identity, "as_dict") else identity
    metadata.update(metadata_overrides)
    (run_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return run_dir


class TestIdentityInputs:
    def test_the_declared_input_order_is_fixed(self) -> None:
        assert IDENTITY_INPUTS == (
            "scenario_id",
            "scenario_version",
            "evidence_policy_version",
            "verifier_rule_version",
            "event_stream_hash",
            "world_version",
            "config_hash",
            "commit",
        )

    def test_every_required_field_is_declared_in_the_input_list(self) -> None:
        for field in REQUIRED_IDENTITY_FIELDS:
            assert field in IDENTITY_INPUTS

    def test_canonical_bytes_are_stable_and_ordered(self) -> None:
        first = canonical_identity_bytes(material_with(), IDENTITY_INPUTS)
        second = canonical_identity_bytes(material_with(), IDENTITY_INPUTS)
        assert first == second
        decoded = json.loads(first)
        assert list(decoded["inputs"]) == list(IDENTITY_INPUTS)

    def test_canonical_bytes_are_independent_of_dict_order(self) -> None:
        shuffled = {name: BASE_MATERIAL[name] for name in reversed(IDENTITY_INPUTS)}
        assert canonical_identity_bytes(shuffled) == canonical_identity_bytes(material_with())

    def test_the_same_material_produces_the_same_hash(self) -> None:
        assert run_identity(material_with()).identity_hash == run_identity(material_with()).identity_hash

    def test_the_hash_algorithm_is_named(self) -> None:
        assert run_identity(material_with()).hash_algorithm == HASH_ALGORITHM
        assert len(run_identity(material_with()).identity_hash) == 64


class TestIdentityVectors:
    """The vectors the Definition of Done names, one assertion each."""

    def test_the_same_events_under_two_scenario_versions_differ(self) -> None:
        first = identity_for("pick-place-red-block", version="1.0")
        entry = dict(REGISTRY["pick-place-red-block@1.0"])
        entry["scenario_version"] = "1.1"
        second = identity_from_entry(entry, event_stream_hash_value="f" * 64, config_hash="a" * 64, commit="b" * 40)
        assert first.identity_hash != second.identity_hash
        assert first.differing_inputs(second) == ("scenario_version",)

    def test_the_same_events_under_two_policy_versions_differ(self) -> None:
        first = identity_for()
        entry = dict(REGISTRY["pick-place-red-block@1.0"])
        entry["evidence_policy"] = entry["evidence_policy"] + " And one more sentence."
        second = identity_from_entry(entry, event_stream_hash_value="f" * 64, config_hash="a" * 64, commit="b" * 40)
        assert first.identity_hash != second.identity_hash
        assert first.differing_inputs(second) == ("evidence_policy_version",)

    def test_a_different_event_stream_changes_only_the_stream_input(self) -> None:
        entry = REGISTRY["pick-place-red-block@1.0"]
        first = identity_from_entry(entry, event_stream_hash_value="1" * 64)
        second = identity_from_entry(entry, event_stream_hash_value="2" * 64)
        assert first.differing_inputs(second) == ("event_stream_hash",)

    def test_two_registered_scenarios_never_share_an_identity(self) -> None:
        hashes = {identity_for(scenario_id).identity_hash for scenario_id in ("pick-place-red-block",)}
        hashes.add(identity_for("kit-three-parts", version="0.2").identity_hash)
        assert len(hashes) == 2

    def test_differing_inputs_reports_in_contract_order(self) -> None:
        first = run_identity(material_with())
        second = run_identity(material_with(scenario_version="2.0", commit="0" * 40))
        assert first.differing_inputs(second) == ("scenario_version", "commit")


class TestDerivedVersions:
    def test_policy_version_tracks_the_prose(self) -> None:
        assert policy_version("one policy") != policy_version("one policy.")
        assert policy_version("one policy") == policy_version("  one policy  ")

    def test_policy_version_is_short_and_prefixed(self) -> None:
        value = policy_version("a policy")
        assert value.startswith("sha256:")
        assert len(value) == len("sha256:") + 16

    def test_verifier_rule_version_tracks_the_entry_point(self) -> None:
        first = verifier_rule_version({"verifier": "a/b.py::verify_one"})
        second = verifier_rule_version({"verifier": "a/b.py::verify_two"})
        assert first != second

    @pytest.mark.parametrize("payload", [None, {}, {"verifier": ""}, {"verifier": 3}])
    def test_a_missing_verifier_is_malformed(self, payload: object) -> None:
        with pytest.raises(RunIdentityError) as caught:
            verifier_rule_version(payload)
        assert caught.value.code == RUN_IDENTITY_MALFORMED

    @pytest.mark.parametrize("text", [None, "", "   ", 3])
    def test_a_missing_policy_text_is_malformed(self, text: object) -> None:
        with pytest.raises(RunIdentityError) as caught:
            policy_version(text)
        assert caught.value.code == RUN_IDENTITY_MALFORMED


class TestIdentityMaterialValidation:
    def test_a_missing_input_fails_closed(self) -> None:
        for field in IDENTITY_INPUTS:
            payload = material_with()
            payload.pop(field)
            with pytest.raises(RunIdentityError) as caught:
                run_identity(payload)
            assert caught.value.code == RUN_IDENTITY_MISSING

    def test_a_blank_input_fails_closed(self) -> None:
        with pytest.raises(RunIdentityError) as caught:
            run_identity(material_with(commit="   "))
        assert caught.value.code == RUN_IDENTITY_MALFORMED

    def test_a_registry_entry_without_an_id_is_refused(self) -> None:
        with pytest.raises(RunIdentityError) as caught:
            identity_from_entry({}, event_stream_hash_value="f" * 64)
        assert caught.value.code == RUN_IDENTITY_MISSING

    def test_a_padded_scenario_id_is_refused(self) -> None:
        entry = dict(REGISTRY["pick-place-red-block@1.0"])
        entry["scenario_id"] = " pick-place-red-block"
        with pytest.raises(RunIdentityError) as caught:
            identity_from_entry(entry, event_stream_hash_value="f" * 64)
        assert caught.value.code == RUN_IDENTITY_MALFORMED

    def test_an_empty_event_stream_hashes_to_a_stable_value(self) -> None:
        assert event_stream_hash(()) == event_stream_hash([])

    def test_event_stream_hash_is_order_sensitive(self) -> None:
        first = [{"event_id": "a"}, {"event_id": "b"}]
        second = [{"event_id": "b"}, {"event_id": "a"}]
        assert event_stream_hash(first) != event_stream_hash(second)

    def test_event_stream_hash_ignores_key_order_within_a_line(self) -> None:
        assert event_stream_hash([{"a": 1, "b": 2}]) == event_stream_hash([{"b": 2, "a": 1}])


class TestEventsFile:
    def test_a_missing_file_is_unreadable(self, tmp_path: Path) -> None:
        with pytest.raises(RunIdentityError) as caught:
            hash_events_file(tmp_path / "absent.jsonl")
        assert caught.value.code == RUN_IDENTITY_EVENTS_UNREADABLE

    def test_a_malformed_line_is_unreadable(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        path.write_text('{"a": 1}\nnot json\n', encoding="utf-8")
        with pytest.raises(RunIdentityError) as caught:
            hash_events_file(path)
        assert caught.value.code == RUN_IDENTITY_EVENTS_UNREADABLE

    def test_a_non_object_line_is_unreadable(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        path.write_text("[1, 2]\n", encoding="utf-8")
        with pytest.raises(RunIdentityError) as caught:
            hash_events_file(path)
        assert caught.value.code == RUN_IDENTITY_EVENTS_UNREADABLE

    def test_blank_lines_are_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        path.write_text('{"a": 1}\n\n{"a": 1}\n', encoding="utf-8")
        assert hash_events_file(path) == event_stream_hash([{"a": 1}, {"a": 1}])


class TestBundleVerification:
    def test_a_matching_bundle_passes(self, tmp_path: Path) -> None:
        identity = identity_for()
        run_dir = bundle(tmp_path, identity=identity)
        verdicts = scan_run_root(ROOT, runs_root=tmp_path, registry=REGISTRY)
        assert len(verdicts) == 1
        assert verdicts[0].ok is True
        assert verdicts[0].identity_hash == identity.identity_hash
        assert run_dir.is_dir()

    def test_a_missing_identity_block_fails(self, tmp_path: Path) -> None:
        bundle(tmp_path, identity=None)
        verdict = scan_run_root(ROOT, runs_root=tmp_path, registry=REGISTRY)[0]
        assert verdict.ok is False
        assert verdict.findings[0].code == RUN_IDENTITY_MISSING

    def test_an_unknown_scenario_fails_rather_than_defaulting(self, tmp_path: Path) -> None:
        entry = dict(REGISTRY["pick-place-red-block@1.0"])
        entry["scenario_id"] = "ghost-scenario"
        entry["scenario_version"] = "9.9"
        identity = identity_from_entry(entry, event_stream_hash_value="f" * 64)
        bundle(tmp_path, identity=identity)
        verdict = scan_run_root(ROOT, runs_root=tmp_path, registry=REGISTRY)[0]
        assert verdict.ok is False
        assert verdict.findings[0].code == RUN_IDENTITY_UNKNOWN_SCENARIO
        assert "ghost-scenario@9.9" in verdict.findings[0].detail

    def test_a_tampered_policy_version_fails(self, tmp_path: Path) -> None:
        identity = identity_for()
        tampered = identity.as_dict()
        tampered["material"]["evidence_policy_version"] = "sha256:deadbeefdeadbeef"
        bundle(tmp_path, identity=tampered)
        verdict = scan_run_root(ROOT, runs_root=tmp_path, registry=REGISTRY)[0]
        assert verdict.ok is False
        codes = {finding.code for finding in verdict.findings}
        assert RUN_IDENTITY_MISMATCH in codes

    def test_a_tampered_verifier_version_fails(self, tmp_path: Path) -> None:
        identity = identity_for()
        tampered = identity.as_dict()
        tampered["material"]["verifier_rule_version"] = "sha256:deadbeefdeadbeef"
        bundle(tmp_path, identity=tampered)
        verdict = scan_run_root(ROOT, runs_root=tmp_path, registry=REGISTRY)[0]
        assert verdict.ok is False
        assert RUN_IDENTITY_MISMATCH in {finding.code for finding in verdict.findings}

    def test_a_hash_that_does_not_match_its_own_material_fails(self, tmp_path: Path) -> None:
        identity = identity_for()
        tampered = identity.as_dict()
        tampered["identity_hash"] = "0" * 64
        bundle(tmp_path, identity=tampered)
        verdict = scan_run_root(ROOT, runs_root=tmp_path, registry=REGISTRY)[0]
        assert verdict.ok is False
        assert any("does not match the hash of its own material" in f.detail for f in verdict.findings)

    def test_missing_identity_material_fields_fail(self, tmp_path: Path) -> None:
        identity = identity_for()
        for field in REQUIRED_IDENTITY_FIELDS:
            tampered = identity.as_dict()
            tampered["material"].pop(field)
            target = tmp_path / f"run-{field}"
            bundle(tmp_path, identity=tampered, name=f"run-{field}")
            verdict = scan_run_root(ROOT, runs_root=target.parent, registry=REGISTRY)
            found = next(v for v in verdict if v.run_id == f"run-{field}")
            assert found.ok is False, field
            assert found.findings[0].code == RUN_IDENTITY_MISSING

    def test_a_false_release_claim_fails(self, tmp_path: Path) -> None:
        bundle(tmp_path, identity=identity_for(), release_eligible=True)
        verdict = scan_run_root(ROOT, runs_root=tmp_path, registry=REGISTRY)[0]
        assert verdict.ok is False
        assert any(finding.code == RUN_IDENTITY_UNSAFE_RELEASE_CLAIM for finding in verdict.findings)

    def test_a_truncated_event_stream_is_a_stream_mismatch(self, tmp_path: Path) -> None:
        identity = identity_for()
        run_dir = bundle(tmp_path, identity=identity)
        events = [{"event_id": "a", "sequence_no": 0}, {"event_id": "b", "sequence_no": 1}]
        (run_dir / "events.jsonl").write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        # The recorded stream hash was for a different stream, so the
        # recomputation from the artifact must disagree.
        verdict = scan_run_root(ROOT, runs_root=tmp_path, registry=REGISTRY)[0]
        assert verdict.ok is False
        assert RUN_IDENTITY_EVENT_STREAM_MISMATCH in {finding.code for finding in verdict.findings}

    def test_an_edited_event_stream_names_the_differing_input(self, tmp_path: Path) -> None:
        entry = REGISTRY["pick-place-red-block@1.0"]
        events = [{"event_id": "a"}, {"event_id": "b"}]
        run_dir = tmp_path / "run-stream"
        run_dir.mkdir(parents=True)
        (run_dir / "events.jsonl").write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        identity = identity_from_entry(
            entry,
            event_stream_hash_value=event_stream_hash(events),
            config_hash="a" * 64,
            commit="b" * 40,
        )
        bundle(tmp_path, identity=identity, name="run-stream", commit="b" * 40, scene_hash="a" * 64)
        # Now edit one line: the artifact no longer matches the recorded hash.
        (run_dir / "events.jsonl").write_text(json.dumps(events[0]) + "\n", encoding="utf-8")
        verdict = scan_run_root(ROOT, runs_root=tmp_path, registry=REGISTRY)[0]
        assert verdict.ok is False
        finding = next(f for f in verdict.findings if f.code == RUN_IDENTITY_EVENT_STREAM_MISMATCH)
        assert "event_stream_hash" in finding.detail

    def test_a_malformed_metadata_file_is_reported(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run-bad"
        run_dir.mkdir(parents=True)
        (run_dir / "metadata.json").write_text("{not json", encoding="utf-8")
        verdict = scan_run_root(ROOT, runs_root=tmp_path, registry=REGISTRY)[0]
        assert verdict.ok is False
        assert verdict.findings[0].code == RUN_IDENTITY_MALFORMED

    def test_a_directory_without_metadata_is_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "not-a-run").mkdir()
        assert scan_run_root(ROOT, runs_root=tmp_path, registry=REGISTRY) == []

    def test_a_missing_runs_root_yields_no_verdicts(self, tmp_path: Path) -> None:
        assert scan_run_root(ROOT, runs_root=tmp_path / "absent", registry=REGISTRY) == []

    def test_recorded_identity_reads_only_an_object(self) -> None:
        assert recorded_identity({"identity": {"a": 1}}) == {"a": 1}
        assert recorded_identity({"identity": "text"}) is None
        assert recorded_identity({}) is None


class TestRealRegistryAndRuns:
    def test_every_registered_scenario_produces_a_distinct_identity(self) -> None:
        identities = [identity_from_entry(entry, event_stream_hash_value="f" * 64) for entry in REGISTRY.values()]
        assert len(identities) >= 5
        assert len({identity.identity_hash for identity in identities}) == len(identities)

    def test_the_identity_block_round_trips_through_json(self) -> None:
        identity = identity_for()
        restored = json.loads(json.dumps(identity.as_dict()))
        assert restored["identity_hash"] == identity.identity_hash
        assert restored["material"] == identity.material

    def test_a_real_scripted_run_records_a_matching_identity(self, tmp_path: Path) -> None:
        """Drive the real sim_cli, then check its real artifact directory."""

        output = tmp_path / "runs"
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools/scripts/sim_cli.py"),
                "run",
                "grasp-failure-001",
                "--runner",
                "scripted",
                "--output-dir",
                str(output),
                "--version",
                "test-identity",
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        run_dirs = [path for path in output.iterdir() if path.is_dir()]
        assert len(run_dirs) == 1
        metadata = json.loads((run_dirs[0] / "metadata.json").read_text(encoding="utf-8"))
        # A legacy fixture under sim/scenarios resolves through its task_id to
        # the registry identity.
        assert metadata["registry_identity"] == "pick-place-red-block@1.0"
        assert metadata["release_eligible"] is False
        assert (run_dirs[0] / "identity.json").is_file()

        verdicts = scan_run_root(ROOT, runs_root=output, registry=REGISTRY)
        assert len(verdicts) == 1
        assert verdicts[0].ok is True, verdicts[0].findings

    def test_two_runs_of_one_scenario_differ_only_by_their_stream(self, tmp_path: Path) -> None:
        entry = REGISTRY["pick-place-red-block@1.0"]
        first = identity_from_entry(entry, event_stream_hash_value="1" * 64)
        second = identity_from_entry(entry, event_stream_hash_value="2" * 64)
        assert first.differing_inputs(second) == ("event_stream_hash",)

    def test_identity_is_reproducible_across_processes(self, tmp_path: Path) -> None:
        script = tmp_path / "compute.py"
        entry_path = Path(ROOT / "sim" / "registry" / "pick-place-red-block.json")
        script.write_text(
            "\n".join(
                [
                    "import sys",
                    f"sys.path.insert(0, {str(ROOT / 'libs' / 'kernel')!r})",
                    "from pathlib import Path",
                    "import json",
                    "from workbench.kernel.scenario_identity import identity_from_entry",
                    f"entry_path = Path({str(entry_path)!r})",
                    "entry = json.loads(entry_path.read_text())",
                    "entry.setdefault('world_version', 'WorkbenchSim-v0')",
                    "identity = identity_from_entry(",
                    "    entry,",
                    "    event_stream_hash_value='f' * 64,",
                    "    config_hash='a' * 64,",
                    "    commit='b' * 40,",
                    ")",
                    "print(identity.identity_hash)",
                ]
            ),
            encoding="utf-8",
        )
        runs = [
            subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=False) for _ in range(2)
        ]
        assert runs[0].returncode == 0, runs[0].stderr
        assert runs[0].stdout.strip() == runs[1].stdout.strip()
        assert runs[0].stdout.strip() == identity_for().identity_hash


class TestDiagnostics:
    def test_every_diagnostic_is_unique(self) -> None:
        assert len(set(EMITTED_CODES)) == len(EMITTED_CODES)

    def test_the_gate_exits_two_when_there_is_nothing_to_inspect(self, tmp_path: Path) -> None:
        from check_run_identity import main

        exit_code = main(["--runs-root", str(tmp_path), "--registry-root", str(ROOT / "sim/registry")])
        assert exit_code == 2

    def test_the_gate_exits_zero_for_a_matching_bundle(self, tmp_path: Path) -> None:
        from check_run_identity import main

        bundle(tmp_path, identity=identity_for())
        assert main(["--runs-root", str(tmp_path)]) == 0

    def test_the_gate_exits_one_for_a_mismatched_bundle(self, tmp_path: Path) -> None:
        from check_run_identity import main

        tampered = identity_for().as_dict()
        tampered["material"]["verifier_rule_version"] = "sha256:deadbeefdeadbeef"
        bundle(tmp_path, identity=tampered)
        assert main(["--runs-root", str(tmp_path)]) == 1

    def test_the_json_summary_names_the_failures(self, tmp_path: Path, capsys) -> None:
        from check_run_identity import main

        bundle(tmp_path, identity=None)
        assert main(["--runs-root", str(tmp_path), "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["exit_code"] == 1
        assert payload["failures"][0]["findings"][0]["code"] == RUN_IDENTITY_MISSING
        assert payload["registry_scenarios"] >= 5
