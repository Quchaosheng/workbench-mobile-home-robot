import argparse
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from _jsonio import JsonInputError, load_json
from _paths import ROOT
from jsonschema import Draft202012Validator

SCHEMA_PATH = ROOT / "tools/schemas/task-packet-v1.schema.json"
PACKET_DIR = ROOT / "docs/task_packets"
PATH_FIELDS = ("allowed_paths", "read_only_paths", "forbidden")
COMMAND_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
PATH_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
COMMAND_TOOLS = frozenset(
    {
        "check_urdf",
        "colcon",
        "docker",
        "git",
        "grep",
        "insmod",
        "make",
        "python",
        "python3",
        "ros2",
        "source",
        "uv",
        "xacro",
    }
)
COMMAND_ENVIRONMENT = frozenset({"PYTEST_DISABLE_PLUGIN_AUTOLOAD"})
COMMAND_SOURCES = frozenset({"/opt/ros/jazzy/setup.bash", "install/setup.bash"})
COMMAND_SUBSTITUTION = "$(ros2 pkg prefix workbench_motion)"


class TaskPacketError(RuntimeError):
    """A Task Packet is malformed or exceeds its Git-visible repository boundary."""


def _format_json_path(parts: list[Any]) -> str:
    return ".".join(str(part) for part in parts) or "<packet>"


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise TaskPacketError(f"duplicate JSON key: {key!r}")
        payload[key] = value
    return payload


def _load_json(path: Path) -> Any:
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError, JsonInputError) as exc:
        raise TaskPacketError(f"cannot read JSON: {exc}") from exc


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _validate_schema(payload: Any) -> None:
    schema = _load_json(SCHEMA_PATH)
    errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda error: list(error.absolute_path))
    if errors:
        details = "; ".join(f"{_format_json_path(list(error.absolute_path))}: {error.message}" for error in errors)
        raise TaskPacketError(f"schema validation failed: {details}")


def _normalize_repo_path(value: str, *, field: str) -> str:
    if value != value.strip():
        raise TaskPacketError(f"{field}: path has leading or trailing whitespace: {value!r}")
    if PATH_CONTROL_CHARACTERS.search(value):
        raise TaskPacketError(f"{field}: path contains a control character: {value!r}")
    if "\\" in value:
        raise TaskPacketError(f"{field}: use repository-relative '/' separators: {value!r}")
    if "[" in value or "]" in value:
        raise TaskPacketError(f"{field}: bracket wildcards are not supported: {value!r}")
    windows_path = PureWindowsPath(value)
    path = PurePosixPath(value)
    if windows_path.drive or windows_path.root or path.is_absolute():
        raise TaskPacketError(f"{field}: absolute paths and drive prefixes are forbidden: {value!r}")
    if not path.parts or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise TaskPacketError(f"{field}: path must be normalized and cannot contain '.' or '..': {value!r}")
    if any("**" in part and part != "**" for part in path.parts):
        raise TaskPacketError(f"{field}: recursive wildcard must be a complete path segment: {value!r}")

    current = ROOT
    has_wildcard = any(any(character in part for character in "*?") for part in path.parts)
    for part in path.parts:
        if any(character in part for character in "*?["):
            break
        candidate = current / part
        if not candidate.exists() and not _is_link(candidate):
            break
        if _is_link(candidate):
            raise TaskPacketError(f"{field}: symbolic links are not authorization boundaries: {value!r}")
        current = candidate
    try:
        current.resolve().relative_to(ROOT.resolve())
    except ValueError as exc:
        raise TaskPacketError(f"{field}: path escapes the repository: {value!r}") from exc
    if has_wildcard and not current.is_dir():
        raise TaskPacketError(f"{field}: wildcard prefix must be an existing directory: {value!r}")
    target = ROOT / path.as_posix()
    if current.is_dir() and (has_wildcard or target.is_dir()):
        for directory, directories, files in os.walk(current, followlinks=False):
            for name in (*directories, *files):
                if _is_link(Path(directory) / name):
                    raise TaskPacketError(f"{field}: directory boundary contains a link: {value!r}")
    return path.as_posix()


def _patterns_overlap(left: str, right: str) -> bool:
    left_parts = tuple(part.casefold() for part in PurePosixPath(left).parts)
    right_parts = tuple(part.casefold() for part in PurePosixPath(right).parts)
    for left_part, right_part in zip(left_parts, right_parts, strict=False):
        if left_part == "**" or right_part == "**":
            return True
        left_wild = any(character in left_part for character in "*?[")
        right_wild = any(character in right_part for character in "*?[")
        if not left_wild and not right_wild and left_part != right_part:
            return False
        if left_wild and not right_wild and not fnmatch.fnmatchcase(right_part, left_part):
            return False
        if right_wild and not left_wild and not fnmatch.fnmatchcase(left_part, right_part):
            return False
    common = min(len(left_parts), len(right_parts))
    return all(part == "**" for part in left_parts[common:]) or all(part == "**" for part in right_parts[common:])


def _validate_boundaries(payload: dict[str, Any]) -> None:
    normalized: dict[str, list[str]] = {}
    for field in PATH_FIELDS:
        normalized[field] = [_normalize_repo_path(value, field=field) for value in payload[field]]
        casefolded = [value.casefold() for value in normalized[field]]
        if len(casefolded) != len(set(casefolded)):
            raise TaskPacketError(f"{field}: paths must be unique on case-insensitive filesystems")

    for value in payload.get("input_refs", []):
        normalized_ref = _normalize_repo_path(value, field="input_refs")
        if "*" in normalized_ref or "?" in normalized_ref:
            raise TaskPacketError(f"input_refs: expected an exact repository path: {value!r}")

    boundary_pairs = (
        ("allowed_paths", "read_only_paths"),
        ("allowed_paths", "forbidden"),
    )
    for left_field, right_field in boundary_pairs:
        for left in normalized[left_field]:
            for right in normalized[right_field]:
                if _patterns_overlap(left, right):
                    raise TaskPacketError(
                        f"boundary overlap: {left_field} entry {left!r} conflicts with {right_field} entry {right!r}"
                    )


def _validate_commands(commands: list[str]) -> None:
    for index, command in enumerate(commands):
        if command != command.strip():
            raise TaskPacketError(f"commands.{index}: command evidence instruction has surrounding whitespace")
        if "\n" in command or "\r" in command or COMMAND_CONTROL_CHARACTERS.search(command):
            raise TaskPacketError(
                f"commands.{index}: command evidence instruction must be one printable line; it is never executed"
            )
        if "`" in command or "$" in command.replace(COMMAND_SUBSTITUTION, ""):
            raise TaskPacketError(f"commands.{index}: unsupported shell expansion")
        if "(then)" in command:
            raise TaskPacketError(f"commands.{index}: split sequential evidence commands into separate entries")
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError as exc:
            raise TaskPacketError(f"commands.{index}: ambiguous command evidence instruction: {exc}") from exc

        words_by_segment: list[list[str]] = [[]]
        for token in tokens:
            if token in {"&&", "|"}:
                if not words_by_segment[-1]:
                    raise TaskPacketError(f"commands.{index}: command segment is empty")
                words_by_segment.append([])
            elif token and all(character in ";&|" for character in token):
                raise TaskPacketError(f"commands.{index}: unsupported shell control operator: {token!r}")
            else:
                words_by_segment[-1].append(token)

        for words in words_by_segment:
            while words and re.fullmatch(r"[A-Z_][A-Z0-9_]*=\S+", words[0]):
                name = words[0].partition("=")[0]
                if name not in COMMAND_ENVIRONMENT:
                    raise TaskPacketError(f"commands.{index}: unapproved environment assignment: {name!r}")
                words.pop(0)
            if words and words[0] == "sudo":
                words.pop(0)
            if not words:
                raise TaskPacketError(f"commands.{index}: command segment is empty")
            executable = "python" if words[0] == "<kicad>/bin/python" else words[0]
            if executable not in COMMAND_TOOLS:
                raise TaskPacketError(f"commands.{index}: unapproved evidence tool: {executable!r}")
            if executable == "git" and (len(words) < 2 or words[1] != "diff"):
                raise TaskPacketError(f"commands.{index}: only read-only 'git diff' evidence is allowed")
            if executable == "source" and (len(words) < 2 or words[1] not in COMMAND_SOURCES):
                raise TaskPacketError(f"commands.{index}: unapproved environment source: {words[1:]!r}")


def _path_matches(pattern: str, path: str) -> bool:
    pattern_parts = PurePosixPath(pattern).parts
    path_parts = PurePosixPath(path).parts

    def match(pattern_index: int, path_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        part = pattern_parts[pattern_index]
        if part == "**":
            return match(pattern_index + 1, path_index) or (
                path_index < len(path_parts) and match(pattern_index, path_index + 1)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], part)
            and match(pattern_index + 1, path_index + 1)
        )

    return match(0, 0)


def _git_output(arguments: list[str]) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *arguments],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise TaskPacketError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout


def _parse_name_status(output: bytes) -> set[str]:
    fields = output.rstrip(b"\0").split(b"\0") if output else []
    paths: set[str] = set()
    index = 0
    try:
        while index < len(fields):
            status = fields[index].decode("ascii")
            index += 1
            path_count = 2 if status[:1] in {"C", "R"} else 1
            for _ in range(path_count):
                paths.add(fields[index].decode("utf-8"))
                index += 1
    except (IndexError, UnicodeDecodeError) as exc:
        raise TaskPacketError("git returned an invalid changed-path record") from exc
    return paths


def _validate_revision(revision: str, *, field: str) -> None:
    if revision.startswith("-") or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", revision):
        raise TaskPacketError(f"invalid {field} revision: {revision!r}")
    _git_output(["rev-parse", "--verify", f"{revision}^{{commit}}"])


def _changed_paths(base: str, head: str | None = None) -> set[str]:
    _validate_revision(base, field="base")
    if head is not None:
        _validate_revision(head, field="head")
        merge_base = _git_output(["merge-base", base, head]).decode("ascii").strip()
    else:
        merge_base = _git_output(["merge-base", base, "HEAD"]).decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", merge_base):
        raise TaskPacketError("git returned an invalid merge-base revision")
    diff_args = ["diff", "--find-renames", "--name-status", "-z"]
    if head is not None:
        return _parse_name_status(_git_output([*diff_args, merge_base, head, "--"]))

    paths = _parse_name_status(_git_output([*diff_args, merge_base, "--"]))
    paths.update(_parse_name_status(_git_output([*diff_args, "--cached", merge_base, "--"])))
    untracked = _git_output(["ls-files", "--others", "--exclude-standard", "-z", "--"])
    try:
        paths.update(path.decode("utf-8") for path in untracked.rstrip(b"\0").split(b"\0") if path)
    except UnicodeDecodeError as exc:
        raise TaskPacketError("git returned a non-UTF-8 untracked path") from exc
    return paths


def _committed_packet_paths() -> list[Path]:
    output = _git_output(["ls-files", "-z", "--", PACKET_DIR.relative_to(ROOT).as_posix()])
    try:
        relative_paths = [path.decode("utf-8") for path in output.rstrip(b"\0").split(b"\0") if path]
    except UnicodeDecodeError as exc:
        raise TaskPacketError("git returned a non-UTF-8 Task Packet path") from exc
    return sorted(ROOT / path for path in relative_paths if PurePosixPath(path).suffix == ".json")


def _is_packet_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return len(parts) >= 3 and parts[:2] == ("docs", "task_packets") and PurePosixPath(path).suffix == ".json"


def _validate_write_boundary(changed_paths: set[str], packets: list[dict[str, Any]]) -> None:
    if not changed_paths:
        return
    changed: list[tuple[str, bool]] = []
    for changed_path in sorted(changed_paths):
        normalized = _normalize_repo_path(changed_path, field="changed path")
        changed.append((normalized, _is_link(ROOT / normalized)))

    rejected: list[list[str]] = []
    for packet in packets:
        unauthorized = [
            path
            for path, is_link in changed
            if is_link or not any(_path_matches(pattern, path) for pattern in packet["allowed_paths"])
        ]
        if not unauthorized:
            return
        rejected.append(unauthorized)

    closest = min(rejected, key=len) if rejected else [path for path, _ in changed]
    raise TaskPacketError(f"no single active Task Packet authorizes every changed path; rejected: {closest}")


def _validate_diff(base: str, head: str | None = None, selected_packets: list[Path] | None = None) -> None:
    changed_paths = _changed_paths(base, head)
    packet_paths = selected_packets or [ROOT / path for path in sorted(changed_paths) if _is_packet_path(path)]
    packet_paths = [path for path in packet_paths if path.is_file() or path.is_symlink()]
    if changed_paths and not packet_paths:
        raise TaskPacketError("changed files require at least one current Task Packet")
    packets = [validate_packet(path) for path in packet_paths]
    _validate_write_boundary(changed_paths, packets)


def validate_packet(packet_path: Path) -> dict[str, Any]:
    try:
        packet_path.resolve().relative_to(ROOT.resolve())
    except ValueError as exc:
        raise TaskPacketError(f"packet file is outside the repository: {packet_path}") from exc
    if packet_path.is_symlink():
        raise TaskPacketError(f"packet file cannot be a symbolic link: {packet_path}")
    payload = _load_json(packet_path)
    _validate_schema(payload)
    _validate_boundaries(payload)
    _validate_commands(payload["commands"])
    return payload


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Task Packets without executing their commands.")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("packet", nargs="?", type=Path, help="one Task Packet JSON file")
    selection.add_argument("--all", action="store_true", help="validate every committed packet in docs/task_packets")
    parser.add_argument("--base", help="also enforce Git-visible changed paths relative to this revision")
    parser.add_argument(
        "--head",
        help="when used with --base, compare against this committed head instead of the checked-out HEAD",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.all:
        packet_paths = _committed_packet_paths()
        if not packet_paths:
            raise TaskPacketError(f"no Task Packets found in {PACKET_DIR}")
    else:
        packet_paths = [args.packet or PACKET_DIR / "example-001-world-reducer.json"]

    failures: list[str] = []
    for packet_path in packet_paths:
        try:
            validate_packet(packet_path)
        except TaskPacketError as exc:
            failures.append(f"{packet_path}: {exc}")
        else:
            print(f"Task Packet validation passed: {packet_path}")
    if failures:
        raise TaskPacketError("Task Packet validation failed:\n" + "\n".join(failures))
    if args.head and not args.base:
        raise TaskPacketError("--head requires --base")
    if args.base:
        _validate_diff(args.base, args.head, None if args.all else packet_paths)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TaskPacketError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from None
