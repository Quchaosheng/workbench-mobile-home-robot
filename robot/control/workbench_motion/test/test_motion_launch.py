"""Source-level launch packaging checks; runtime evidence is separate."""

import ast
from pathlib import Path
from xml.etree import ElementTree

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_optional_gui_arguments_keep_existing_headless_server():
    source = (ROOT / "launch" / "sim_control.launch.py").read_text()
    tree = ast.parse(source)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    declarations = {
        n.args[0].value: {kw.arg: kw.value.value for kw in n.keywords}
        for n in calls
        if isinstance(n.func, ast.Name) and n.func.id == "DeclareLaunchArgument"
    }
    assert declarations["gui"]["default_value"] == "false"
    assert declarations["rviz"]["default_value"] == "false"
    assert '"-s -r -v 3 empty.sdf"' in source
    assert '"-g -v 3"' in source
    config = yaml.safe_load((ROOT / "config" / "motion.rviz").read_text())
    assert config["Visualization Manager"]["Global Options"]["Fixed Frame"] == "world"
    deps = [n.text for n in ElementTree.parse(ROOT / "package.xml").findall("exec_depend")]
    assert "rviz2" in deps
    assert "rosidl_runtime_py" in deps
    setup = (ROOT / "setup.py").read_text()
    assert 'glob("config/*.rviz")' in setup
    assert "motion_benchmark = workbench_motion.motion_benchmark:main" in setup
