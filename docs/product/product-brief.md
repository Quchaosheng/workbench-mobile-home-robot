# Workbench Product Brief

Status: hypothesis baseline; validate with external users before treating as a market claim.

## Product statement

Workbench is an evidence-first runtime for robot task execution. It constrains
agent behavior to semantic actions, records execution and observation evidence,
verifies the resulting world state, and provides deterministic replay and a
read-only operational view.

## Initial ICP hypotheses

The first users to validate are small robot teams and research labs that:

- build or deploy ROS-based mobile robots, arms, or household-task prototypes;
- already have a real robot or repeatable simulation task;
- lose time in task configuration, deployment, failure diagnosis, or proving that a task actually completed;
- can nominate a technical evaluator and provide a bounded test environment.

These are hypotheses, not a final market definition. Do not expand the target
audience until interviews show a repeated problem and a credible path to use.

### Interview cohort rule

Start with **8-10 effective interviews** across at least three of the groups
above. The group counts are a sampling guide, not four mandatory quotas. An
effective interview includes a recent concrete task or failure, current
workaround, impact, and a willingness (or refusal) to test a bounded task.
Record refusals and non-fit users; they are evidence about the boundary of the
ICP rather than missing work.

## Personas to validate

| Persona | Job to be understood | Evidence to collect |
|---|---|---|
| Robot developer / operator | Configure, run, diagnose, and repeat a task without hidden state | install time, task steps, failure recovery, logs used |
| Technical lead / lab lead | Reduce integration risk and make results comparable across runs | interfaces, reproducibility needs, review burden, deployment constraints |
| Project or business owner | Decide whether the workflow is worth continued investment | time saved, risk reduced, team capacity, procurement path |

## Candidate jobs

- When a robot task reports success, determine whether the physical or simulated state actually satisfies the goal.
- When a task fails, identify whether the cause is perception, action, evidence, policy, or environment without guessing.
- When a scenario changes, replay the old result and compare it without mixing versions or evidence classes.
- When onboarding a new operator or team, complete a bounded task from a clean environment with clear failure feedback.

## Current problem hypotheses

1. Action completion is often mistaken for task completion.
2. Failure evidence is scattered across logs, screenshots, and operator memory.
3. Scenario-specific code tends to duplicate policy, event, and verification logic.
4. Scripted demos are easier to produce than truthful evidence about Gazebo or physical execution.
5. New users need a reproducible installation and first-task path before they can provide useful feedback.

Each hypothesis needs a problem card with a real user quote, recent example,
frequency, impact, current workaround, and a falsification condition.

## Product promise for the current phase

For the current software phase, promise only:

- deterministic offline and scripted runtime behavior within documented boundaries;
- explicit `confirmed`, `refuted`, and `insufficient_evidence` outcomes;
- replayable event and evidence artifacts;
- read-only inspection of runs and limitations.

Do not promise completed Gazebo task worlds, physical robot capability, or
general-purpose autonomous household operation until their evidence gates pass.

The product baseline behind this promise is re-scoped in
[ADR-0007](../decisions/ADR-0007-household-mvp-baseline.md): the household MVP is
one mobile base, one arm/gripper, one RGB-D camera, one onboard compute and one
safety MCU, bounded to issues #159, #163, #164 and #152. That record is
`proposed`; it grants no scope, procurement or release authority until its owner
approval register is complete. The fixed tabletop simulator (ADR-0001) and the
fixed UR5e development bench (ADR-0004) remain separate stages and are not the
household product.

## Non-goals

- A universal robot controller or motion-planning replacement.
- A CRM or storage location for identifiable customer data.
- A marketplace for arbitrary scenario plugins.
- A claim that a user interview, screenshot, or scripted fixture proves physical success.

## Validation plan

Run a small, diverse set of interviews before committing to a new scenario:

- 2-4 research-lab or university users;
- 2-4 small robot-team members;
- 1-3 integrators or field implementers;
- 1-3 independent ROS/robot developers.

For each conversation, record a problem card rather than a feature wish list.
Promote a problem only when multiple records show the same job, failure, or
cost, and at least one participant is willing to test a bounded task.

## Product decision gates

| Gate | Minimum evidence | Decision allowed |
|---|---|---|
| Problem promotion | One concrete example, one source reference, and a falsification condition | Investigate or reject |
| Roadmap promotion | Repeated pattern across independent participants or runs, measurable impact, and a named evaluator | Prototype or create a bounded Issue |
| Scenario trial | Known starting state, semantic actions, measurable goal, failure matrix, and evidence owner | Schedule a Design Partner or simulation trial |
| Release claim | Engineering gate plus eligible evidence class and reproducible artifact | Publish the scoped claim only |

These are default decision gates. A lower sample size may be accepted only when
the Project Owner records the reason, risk, and follow-up evidence in the
decision log.

## Decision rule

Prioritize a scenario when it has a repeated user problem, a named evaluator,
a bounded real task, a measurable success condition, and an evidence path that
does not require weakening shared safety or verification boundaries.
