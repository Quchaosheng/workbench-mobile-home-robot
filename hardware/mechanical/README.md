# Mechanical engineering package

This directory is the source-controlled mechanical concept for the Workbench
Home Robot, Revision D. It is no longer the 280 x 240 x 330 mm tabletop shell.
The current target is a 540 x 520 mm mobile base with a 350 mm braked liftable torso,
a continuous mineral-white utility body, two seven-axis arms, an 18 L parcel bay,
and a locked quick-change tool system.

## Authoritative baseline

`design-spec.json`, `cad/desk_robot.scad`, and the generated STEP package use one
coordinate system: `robot_base` has +X to robot right, +Y to robot rear, +Z up,
and ground is Z=0. The base top is Z=140 mm and the checked-in assembly is the
raised working pose with a 1450 mm head-top envelope. The stowed head-top
envelope is 1100 mm and the controlled lift travel is 350 mm. Generated
geometry is measured during export and the generator fails if the STEP top does
not match the controlled envelope.

`design-spec.json#components` is the sole authoritative Revision D mass source
(`REV-D-MASS-001`). Each component has a stable ID, kg/mm values in
`robot_base`, source, uncertainty, status, and inclusion rule. The current
`mass-ledger.csv` is a checked mirror; `mass-ledger-legacy.csv` preserves the
superseded 55 kg planning rows and is never included in release calculations.
Product, Mechanical, Hardware, and Safety approval is explicitly required in
`mass-model-approval-register.csv` before any release decision.

The Revision D chassis is four independent 140 mm steer-drive modules. Any
document describing two 200 mm driven wheels, four support casters, the earlier
55 kg workbench, or a Rev B/Rev C fixed shape is a legacy planning reference and
must not be used as the current mechanical or procurement interface.

## Product architecture

- **Mobile base:** four independent steer-drive modules provide longitudinal,
  lateral, diagonal, and rotate-in-place self-motion. Each module has a 140 mm
  non-marking wheel, absolute steering encoder, drive encoder, 30 mm suspension,
  and a normally-closed brake. Stabilizer feet remain flush during navigation
  and deploy only for stationary manipulation.
- **Lift:** four guides, two synchronized screws, two normally-closed brakes,
  two mechanical lock pins, dual encoders, hard limits, and pinch detection.
- **Arms:** each arm has J1 base yaw, J2 shoulder pitch, J3 shoulder roll,
  J4 elbow pitch, J5 forearm roll, J6 wrist pitch, and J7 tool roll. The per-arm
  planning envelope is 2 kg at 650 mm reach or 3 kg at 400 mm at reduced speed; these are not yet
  certified performance claims.
- **Tools:** adaptive parcel gripper, compliant brush/dry-mop head, and a
  removable 316L/PEEK/silicone induction-cooking tool. Cooking is supervised,
  induction-only, and excludes open flame, boiling-liquid carry, and hot-pan
  transport.

## Industrial design and CMF

The consumer-facing surface is a continuous warm mineral-white shell with hidden
primary parting lines. The structural waist, lift, and arm links use bead-blasted
graphite anodized aluminium; the face is a single smoked strengthened-glass
lens; the parcel-bay/acoustic insert is graphite 3D-knit recycled PET. Jade or
warm amber is reserved for one status light. Visible glossy plastic, exposed
fasteners, decorative color blocks, toy-like antennae, and unguarded wheel
mechanisms are out of scope. The four wheel treads remain visually readable so
the product clearly communicates self-motion, while the steering bearings and
cabling stay guarded inside the base skirt.

The head uses a wide rounded-rectangle expression window inside a soft white
frame. It is not a floating shell: a dedicated neck mount has a load-bearing
pedestal, broad shoulder plate, keyed head register, four hidden M6 fasteners,
two dowel pins, and a 32 mm central cable passage. The head is lifted onto the
register after the harness is connected and can be removed vertically after
the rear cover and underside fasteners are released. Both shoulder centres are
mounted in the torso side walls below that neck; neither arm supports or
visually frames the head.

## Reproduce

```bash
python hardware/mechanical/tools/generate_artifacts.py
```

Every `NAME = expression` constant in `cad/desk_robot.scad` must be classified in
`cad/scad-parameter-manifest.json` as spec-backed (`shared_parameters`), derived
(`derived_parameters`), or visual-only (`visual_only_parameters` with a reason and
owner). `generate_artifacts.py` evaluates the SCAD arithmetic with a restricted
AST walk and fails with a non-zero exit when a shared value drifts from
`design-spec.json`, when a constant is unclassified, or when the manifest is
missing, duplicated, or revision-mismatched. Change the design dimensions only in
`design-spec.json`; never edit SCAD values to make the check pass.

The command regenerates the analytical report, C revision general arrangement,
thermal path, drop screen, BOM, assembly sequence, and CadQuery STEP package.
It intentionally reports `CONCEPT_PHYSICAL_VALIDATION_REQUIRED`: no rendering
or analytical result substitutes for lift synchronization, arm sweep, thermal,
stability, force-limit, or guarded household-task tests on a serialized unit.

- `generated/enclosure.step`: torso exchange solid for supplier review.
- `generated/desk_robot_assembly.step`: raised working pose with deployed
  stabilizers, mobile base, lift, torso, head, dual 7R arms, and tool dock.
- `generated/desk_robot_navigation_low.step`: lowered navigation pose with
  stabilizers stowed inside the base envelope.
- `generated/desk_robot_exploded.step`: exploded assembly for work instructions.
- `generated/parts/*.step`: ten D revision concept parts, including the separate neck mount.
- `generated/drawings/general-arrangement.svg`: D revision architecture and lift states.
- `generated/drawings/thermal-flow.svg`: isolated electronics airflow path.
- `generated/analysis.json`: hashed mass model, per-pose CG, four-direction drive/stabilized tip screens, payload moment, clearances, and the OpenSCAD parameter comparison.
- `analysis.schema.json`: contract for the generated analytical evidence.
- `generated/bom-manifest.json`: mass-model revision/hash binding for the generated BOM.
- `revision-d-architecture.md`: bimanual workspace, task boundary, and architecture rationale.
