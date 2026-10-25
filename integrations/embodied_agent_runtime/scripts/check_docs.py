#!/usr/bin/env python3
import pathlib
import re
import subprocess


ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCUMENTS = (
    ROOT / "README.runtime.md",
    ROOT / "README.runtime.zh-CN.md",
    ROOT / "ros2_ws/src/runtime_monitor/README.md",
    ROOT / "ros2_ws/src/runtime_monitor/README.zh-CN.md",
)
LINK = re.compile(r"\]\(([^)]+)\)")


def local_targets(document):
    in_fence = False
    for line_number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in LINK.finditer(line):
            target = match.group(1).split("#", 1)[0]
            if target and not target.startswith(("http://", "https://", "mailto:")):
                yield line_number, target


for document in DOCUMENTS:
    assert document.is_file() and document.stat().st_size > 0, document
    for line_number, target in local_targets(document):
        resolved = (document.parent / target).resolve()
        assert resolved.exists(), f"{document.relative_to(ROOT)}:{line_number}: missing {target}"

assert "(README.runtime.zh-CN.md)" in DOCUMENTS[0].read_text(encoding="utf-8")
assert "(README.runtime.md)" in DOCUMENTS[1].read_text(encoding="utf-8")
assert "(README.zh-CN.md)" in DOCUMENTS[2].read_text(encoding="utf-8")
assert "(README.md)" in DOCUMENTS[3].read_text(encoding="utf-8")

chinese_readme = DOCUMENTS[1].read_text(encoding="utf-8")
for required in (
    "### Action 分层与 TaskEvent",
    "ExecuteDeviceCommand",
    "docs/cancel-stop-sequence.md",
    "### Docker 软件 E2E",
    "make docker-build",
    "### ArUco 输入",
    "DICT_4X4_50",
):
    assert required in chinese_readme, f"Chinese README is missing {required}"

tracked = subprocess.check_output(
    ["git", "ls-files", "-z"], cwd=ROOT
).decode("utf-8").split("\0")
for path in tracked:
    normalized = f"/{path.lower().strip('/')}"
    assert "/handoff/" not in normalized, f"private handoff material is tracked: {path}"
    assert not normalized.endswith((".pem", ".key")), f"private key is tracked: {path}"
    assert "/credential/" not in normalized and "/credentials/" not in normalized, (
        f"credential material is tracked: {path}"
    )

print("PASS: bilingual documentation and repository boundaries")
