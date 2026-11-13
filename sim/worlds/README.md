# sim/worlds

Gazebo world files (.sdf) for each scenario class, and the versions that resolve
to them.

```
worlds/
  workbench_tabletop_v0.sdf     WorkbenchSim-v0    frozen tabletop scenarios
  workbench_household_v0.2.sdf  WorkbenchSim-v0.2  household / Revision D scenarios
```

A scenario manifest names a world through its `world_version` field, and
`tools/scripts/validate_scenarios.py` resolves that string to a committed file
**at load**. Before that map existed, `world_version` was a string nothing
resolved: a manifest could name a world that was never committed, and the failure
surfaced at run time as a missing model URI rather than as a validation error.

## The two worlds are deliberately different

`WorkbenchSim-v0` is the frozen single-arm tabletop the twelve `task-place-red-block`
manifests were written against. `WorkbenchSim-v0.2` is the 6 x 5 m household room
for the Revision D bimanual robot. Spawning one in the other would put the tray,
the block and the shelf metres from where the scenario expects them, which is why
the version is part of the frozen fixture rather than a setting.

## Changing a world

Any geometry change requires a **new** `world_version` and a re-freeze of the
affected manifests. The scene hash and the committed occupancy map derive from
these poses, so an edited file under an old version would make a run's recorded
hash describe a world that no longer exists.

## What a world file does not claim

A world is geometry, not evidence. A run in one is `gazebo` evidence at best, and
the readiness report decides eligibility; every evidence table keeps the physical
column `NOT_EXECUTED`. Nothing here asserts the physical robot was built, that a
controller is tuned, or that any motion is safe.

## Validating

```bash
python3 tools/scripts/validate_scenarios.py   # resolves every manifest's world_version
python3 -m pytest tests/unit/test_world_versions.py
gz sdf -k sim/worlds/workbench_household_v0.2.sdf   # needs Gazebo installed
```

The last command is the only one that checks the SDF against the simulator's own
schema. It needs Gazebo Harmonic, which the repository's CI container has and a
plain Python environment does not, so the tests parse the file as XML and assert
structure. A structural pass is not a physics pass.
