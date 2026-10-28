import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def enable_local_packages() -> Path:
    # `workbench` is a regular package under both libs/kernel and libs/application,
    # so the first path entry wins for the whole process and a caller importing
    # workbench.kernel gets whichever tree came first. Inserting in reverse order
    # leaves libs/kernel ahead of libs/application (issue #425).
    for relative_path in [
        "firmware/virtual_mcu",
        "services/world_model",
        "services/backend",
        "services/agent_runtime",
        "libs/contracts",
        "libs/application",
        "libs/kernel",
    ]:
        path = str(ROOT / relative_path)
        while path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)
    return ROOT
