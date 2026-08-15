# Require Tier-Specific Experiment Artifacts

Observation exercises, bounded change labs, and evidence projects will have distinct minimum acceptance artifacts. Observation exercises require a reviewable source-and-runtime trace; bounded change labs add a scoped patch, invariants, normal and failure oracles, cleanup checks, and focused, related, and full regressions; evidence projects add deterministic triggers, applicable `S/F/B/C/R` evidence, resource accounting, isolated reproduction, and an explicit account of what the evidence cannot establish.

## Considered Options

- Accept reading answers for observation work.
- Accept a successful feature test or full `usertests` run for implementation work.
- Accept long random stress runs as evidence-level validation.

## Consequences

Experiment templates and review rubrics must request the artifact appropriate to their tier. Panic, crash, or destructive-image cases run in isolated QEMU instances or temporary images, and recovery claims include an idempotency check.
