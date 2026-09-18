"""Seeded property suites for the fail-closed boundaries named by Issue #89.

The suites exercise packages from this checkout, and they must keep working
when the repository is copied into a throwaway sandbox for a mutation probe, so
the local package paths are inserted here rather than relying on the editable
install.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for relative_path in (
    "libs/contracts",
    "libs/kernel",
    "libs/hardware",
    "services/agent_runtime",
    "services/world_model",
    "firmware/virtual_mcu",
):
    local_path = str(ROOT / relative_path)
    if local_path not in sys.path:
        sys.path.insert(0, local_path)
