import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "tools/scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("check_task_packet", SCRIPTS / "check_task_packet.py")
assert SPEC and SPEC.loader
task_packet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(task_packet)


@pytest.fixture
def valid_packet() -> dict[str, object]:
    return {
        "issue": 106,
        "human_owner": "owner",
        "objective": "Enforce a bounded task.",
        "allowed_paths": ["tools/scripts/check_task_packet.py", "tests/unit/**"],
        "read_only_paths": ["interfaces/**"],
        "forbidden": ["robot/control/**", "firmware/**"],
        "acceptance": ["adversarial tests pass"],
        "commands": ["python -m pytest tests/unit -k task_packet -v"],
        "evidence": ["pytest output"],
        "stop_conditions": ["interface change required"],
    }


def test_valid_packet_passes_schema_boundaries_and_commands(valid_packet: dict[str, object]) -> None:
    task_packet._validate_schema(valid_packet)
    task_packet._validate_boundaries(valid_packet)
    task_packet._validate_commands(valid_packet["commands"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issue", 0),
        ("issue", ""),
        ("human_owner", "   "),
        ("objective", ["not", "a", "scalar"]),
        ("allowed_paths", "tools/**"),
        ("acceptance", [True]),
        ("commands", []),
        ("evidence", {"report": "output"}),
        ("stop_conditions", None),
    ],
)
def test_schema_rejects_malformed_or_empty_fields(valid_packet: dict[str, object], field: str, value: object) -> None:
    payload = copy.deepcopy(valid_packet)
    payload[field] = value
    with pytest.raises(task_packet.TaskPacketError, match="schema validation failed"):
        task_packet._validate_schema(payload)


def test_schema_rejects_missing_and_unknown_fields(valid_packet: dict[str, object]) -> None:
    missing = copy.deepcopy(valid_packet)
    del missing["human_owner"]
    with pytest.raises(task_packet.TaskPacketError, match="human_owner"):
        task_packet._validate_schema(missing)

    extra = copy.deepcopy(valid_packet)
    extra["unapproved_scope"] = ["services/**"]
    with pytest.raises(task_packet.TaskPacketError, match="Additional properties"):
        task_packet._validate_schema(extra)


def test_schema_accepts_and_validates_v1_governance_fields(valid_packet: dict[str, object]) -> None:
    payload = copy.deepcopy(valid_packet)
    payload.update(
        {
            "decision_supported": "Can this change stay inside its approved boundary?",
            "input_refs": ["docs/AI-NATIVE-PLAYBOOK.md"],
            "outputs": ["implementation", "tests"],
            "max_iterations": 2,
            "data_classification": "public",
            "model_policy": "external_allowed",
        }
    )
    task_packet._validate_schema(payload)
    task_packet._validate_boundaries(payload)

    payload["max_iterations"] = 0
    with pytest.raises(task_packet.TaskPacketError, match="max_iterations"):
        task_packet._validate_schema(payload)


def test_input_refs_must_be_exact_repository_paths(valid_packet: dict[str, object]) -> None:
    payload = copy.deepcopy(valid_packet)
    payload["input_refs"] = ["docs/**"]
    task_packet._validate_schema(payload)
    with pytest.raises(task_packet.TaskPacketError, match="exact repository path"):
        task_packet._validate_boundaries(payload)


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "tools/../outside",
        "tools//outside",
        "tools/./outside",
        "/absolute/path",
        "C:/outside",
        "C:\\outside",
        " tools/**",
        "tools/** ",
        "tools/a**/file.py",
        "docs/[",
        "docs/[]",
        "docs/control\nfile.py",
        "docs/control\x00file.py",
    ],
)
def test_path_validation_rejects_escape_and_ambiguous_forms(path: str) -> None:
    with pytest.raises(task_packet.TaskPacketError):
        task_packet._normalize_repo_path(path, field="allowed_paths")


def test_path_validation_rejects_existing_symbolic_link_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = tmp_path / "link"
    link.mkdir()
    original_is_symlink = Path.is_symlink

    def pretend_link(path: Path) -> bool:
        return path == link or original_is_symlink(path)

    monkeypatch.setattr(task_packet, "ROOT", tmp_path)
    monkeypatch.setattr(Path, "is_symlink", pretend_link)
    with pytest.raises(task_packet.TaskPacketError, match="symbolic links"):
        task_packet._normalize_repo_path("link/output.json", field="allowed_paths")


def test_path_validation_rejects_link_below_wildcard_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allowed = tmp_path / "allowed"
    link = allowed / "nested" / "link"
    link.mkdir(parents=True)
    monkeypatch.setattr(task_packet, "ROOT", tmp_path)
    monkeypatch.setattr(task_packet, "_is_link", lambda path: path == link)
    with pytest.raises(task_packet.TaskPacketError, match="directory boundary contains a link"):
        task_packet._normalize_repo_path("allowed/**", field="allowed_paths")


def test_path_validation_allows_future_paths_but_rejects_changed_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_packet, "ROOT", tmp_path)
    assert task_packet._normalize_repo_path("new/path/**", field="allowed_paths") == "new/path/**"
    link = tmp_path / "new"
    link.mkdir()
    monkeypatch.setattr(task_packet, "_is_link", lambda path: path == link)
    with pytest.raises(task_packet.TaskPacketError, match="symbolic links"):
        task_packet._validate_write_boundary({"new"}, [{"allowed_paths": ["new/**"]}])


@pytest.mark.parametrize(
    ("allowed", "protected"),
    [
        ("interfaces/**", "interfaces/**"),
        ("interfaces/json_schema/file.json", "interfaces/**"),
        ("tests/**", "tests/unit/**"),
        ("tests/*", "tests/unit/**"),
    ],
)
def test_boundary_validation_rejects_unsafe_overlap(
    valid_packet: dict[str, object], allowed: str, protected: str
) -> None:
    payload = copy.deepcopy(valid_packet)
    payload["allowed_paths"] = [allowed]
    payload["forbidden"] = [protected]
    with pytest.raises(task_packet.TaskPacketError, match="boundary overlap"):
        task_packet._validate_boundaries(payload)


def test_read_only_and_forbidden_overlap_preserves_a_protected_input(valid_packet: dict[str, object]) -> None:
    payload = copy.deepcopy(valid_packet)
    payload["read_only_paths"] = ["interfaces/**"]
    payload["forbidden"] = ["interfaces/**"]
    task_packet._validate_boundaries(payload)


def test_forbidden_path_with_spaces_cannot_hide_inside_allowed_boundary(valid_packet: dict[str, object]) -> None:
    payload = copy.deepcopy(valid_packet)
    payload["allowed_paths"] = ["docs/**"]
    payload["forbidden"] = ["docs/private files/**"]
    with pytest.raises(task_packet.TaskPacketError, match="boundary overlap"):
        task_packet._validate_boundaries(payload)


def test_command_is_data_and_unsafe_multiline_ambiguity_is_reported(valid_packet: dict[str, object]) -> None:
    commands = copy.deepcopy(valid_packet["commands"])
    commands.append("make test\nrm output")
    with pytest.raises(task_packet.TaskPacketError, match="never executed"):
        task_packet._validate_commands(commands)


@pytest.mark.parametrize(
    "command",
    [
        "do something",
        "python -c 'unterminated",
        " make test",
        "python -m pytest; rm -rf outside",
        "make test && curl https://example.invalid",
        "git reset --hard",
        "/tmp/make test",
        "BASH_ENV=/tmp/evil make test",
        "python -m pytest $(curl https://example.invalid)",
        "source /tmp/arbitrary.sh",
    ],
)
def test_command_validation_rejects_arbitrary_or_ambiguous_text(command: str) -> None:
    with pytest.raises(task_packet.TaskPacketError, match="command"):
        task_packet._validate_commands([command])


def test_command_validation_accepts_approved_evidence_wrappers() -> None:
    task_packet._validate_commands(
        [
            "git diff --check",
            "sudo make -C kernel/wbcan test",
            "sudo insmod kernel/wbcan/wbcan.ko fail_debugfs=1",
            "source /opt/ros/jazzy/setup.bash && colcon build",
            "python -c 'print(\"quoted | text\")'",
        ]
    )


def test_validate_packet_reads_a_valid_repository_packet() -> None:
    task_packet.validate_packet(ROOT / "docs/task_packets/example-001-world-reducer.json")


def test_all_mode_validates_each_packet_without_executing_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet_dir = tmp_path / "packets"
    packet_dir.mkdir()
    for name in ("one.json", "two.json"):
        (packet_dir / name).write_text("{}", encoding="utf-8")
    seen: list[str] = []
    monkeypatch.setattr(task_packet, "_committed_packet_paths", lambda: sorted(packet_dir.glob("*.json")))
    monkeypatch.setattr(task_packet, "validate_packet", lambda path: seen.append(path.name))
    assert task_packet.main(["--all"]) == 0
    assert seen == ["one.json", "two.json"]


def test_invalid_json_reports_a_validation_error(tmp_path: Path) -> None:
    packet = tmp_path / "bad.json"
    packet.write_text("{", encoding="utf-8")
    with pytest.raises(task_packet.TaskPacketError, match="cannot read JSON"):
        task_packet._load_json(packet)


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    packet = tmp_path / "duplicate.json"
    packet.write_text('{"allowed_paths": [], "allowed_paths": ["robot/**"]}', encoding="utf-8")
    # The shared reader names the offending key; the phrase matches what every
    # other evidence boundary reports (issue #451).
    with pytest.raises(task_packet.TaskPacketError, match="duplicate JSON object key"):
        task_packet._load_json(packet)


def test_name_status_includes_both_sides_of_renames() -> None:
    output = b"M\0changed.py\0D\0deleted.py\0R100\0old.py\0new.py\0"
    assert task_packet._parse_name_status(output) == {"changed.py", "deleted.py", "old.py", "new.py"}


def test_changed_paths_tracks_final_git_visible_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Task Packet Test")
    git("config", "user.email", "task-packet@example.invalid")
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    for name in ("deleted.py", "modified.py", "old.py", "reverted.py", "staged.py"):
        (tmp_path / name).write_text(f"base {name}\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")

    (tmp_path / "added.py").write_text("added\n", encoding="utf-8")
    (tmp_path / "modified.py").write_text("modified\n", encoding="utf-8")
    (tmp_path / "reverted.py").write_text("temporary change\n", encoding="utf-8")
    (tmp_path / "deleted.py").unlink()
    git("mv", "old.py", "new.py")
    git("add", "-A")
    git("commit", "-qm", "change files")

    (tmp_path / "reverted.py").write_text("base reverted.py\n", encoding="utf-8")
    (tmp_path / "staged.py").write_text("staged\n", encoding="utf-8")
    git("add", "reverted.py", "staged.py")
    (tmp_path / "staged.py").write_text("unstaged after staged\n", encoding="utf-8")
    (tmp_path / "untracked.py").write_text("untracked\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("ignored\n", encoding="utf-8")

    monkeypatch.setattr(task_packet, "ROOT", tmp_path)
    assert task_packet._changed_paths(base) == {
        "added.py",
        "deleted.py",
        "modified.py",
        "new.py",
        "old.py",
        "staged.py",
        "untracked.py",
    }


def test_changed_paths_can_target_pr_head_from_merge_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Task Packet Test")
    git("config", "user.email", "task-packet@example.invalid")
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")

    (tmp_path / "main.txt").write_text("main\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "main update")
    git("checkout", "-qb", "pr", base)
    (tmp_path / "pr.txt").write_text("pr\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "pr update")
    head = git("rev-parse", "HEAD")

    monkeypatch.setattr(task_packet, "ROOT", tmp_path)
    assert task_packet._changed_paths(base, head) == {"pr.txt"}


def test_write_boundary_checks_every_changed_path(valid_packet: dict[str, object]) -> None:
    task_packet._validate_write_boundary(
        {"tools/scripts/check_task_packet.py", "tests/unit/test_task_packet.py"},
        [valid_packet],
    )
    with pytest.raises(task_packet.TaskPacketError, match=r"robot/control/dispatcher\.py"):
        task_packet._validate_write_boundary(
            {"tools/scripts/check_task_packet.py", "robot/control/dispatcher.py"},
            [valid_packet],
        )


def test_write_boundary_does_not_union_separate_packet_permissions(valid_packet: dict[str, object]) -> None:
    other_packet = copy.deepcopy(valid_packet)
    other_packet["allowed_paths"] = ["robot/control/**"]
    other_packet["forbidden"] = ["tools/**"]
    task_packet._validate_boundaries(valid_packet)
    task_packet._validate_boundaries(other_packet)

    with pytest.raises(task_packet.TaskPacketError, match="no single active Task Packet"):
        task_packet._validate_write_boundary(
            {"tools/scripts/check_task_packet.py", "robot/control/dispatcher.py"},
            [valid_packet, other_packet],
        )


def test_path_matching_does_not_let_single_star_cross_directories() -> None:
    assert task_packet._path_matches("apps/dashboard/*.js", "apps/dashboard/app.js")
    assert not task_packet._path_matches("apps/dashboard/*.js", "apps/dashboard/vendor/app.js")
    assert task_packet._path_matches("apps/dashboard/**", "apps/dashboard/vendor/app.js")
