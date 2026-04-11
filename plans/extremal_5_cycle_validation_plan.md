# Extremal 5-Cycle Validation Plan

## Objective

Demonstrate that a clean reseed using the new bwrap isolation system can run through 5 cycles without filesystem escape or sandbox-induced instability, then pause for review.

## Clean-start protocol

1. stop any existing `extremal` run
2. wipe the repo
3. reseed from the paper
4. confirm sandbox is enabled in the generated config
5. confirm the worker cannot read a sibling repo from inside the sandbox before the supervisor starts
6. start the supervisor

## Per-cycle monitoring checklist

For each cycle:
- note phase, attempt number, and whether the run resumed mid-cycle
- inspect worker prompt
- inspect worker output
- inspect deterministic validation result
- inspect all correspondence outputs
- inspect soundness outputs if any
- inspect reviewer decision
- record the substantive tablet changes
- record any sandbox-related issue, even if recovered

## Explicit sandbox checks

At cycle start and after any restart:
- verify `lagentworker` inside bwrap can read the project
- verify `lagentworker` inside bwrap cannot read `/home/leanagent/math/extremal_vectors_tablets`
- verify `lagentworker` inside bwrap cannot read `/home/leanagent/src/lagent-tablets`

## Restart criteria

Restart from a clean reseed if any of these occur:
- worker or verifier can still read sibling/source paths
- project-local runtime snapshot is missing required files
- worker prompts or scripts point outside the project runtime by design
- sandbox breaks agent launch or deterministic checks in a structural way

Do not restart for ordinary mathematical failures. Those are part of normal cycle behavior.

## Pause point

When committed cycle 5 is reached cleanly:
- stop at the cycle boundary
- leave the repo intact
- summarize all 5 cycles, all reviewer decisions, all verification outputs, and any code fixes made during the run
