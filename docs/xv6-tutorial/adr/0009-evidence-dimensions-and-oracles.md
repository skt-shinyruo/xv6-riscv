# Use Explicit Evidence Dimensions and Correctness Oracles

Tutorial experiments will classify evidence as static reasoning (`S`), normal function (`F`), boundary or failure behavior (`B`), concurrency (`C`), and recovery or persistence (`R`). Each task must state a correctness oracle with expected outcomes, allowed side effects, resource behavior, and recovery behavior; timeouts, boot success, and a passing general test suite are supplementary evidence rather than standalone correctness claims.

## Considered Options

- Treat a zero exit status, successful boot, or full `usertests` run as the universal acceptance rule.
- Require every experiment to cover every evidence dimension regardless of its claim.
- Leave evidence categories and oracle strength to individual lab authors.

## Consequences

Each experiment must declare applicable dimensions and justify `N/A` dimensions. Concurrency claims require deterministic or bounded interleavings and appropriate hart configurations; persistence claims require isolated images, explicit crash points, recovery checks, and idempotent restart evidence. Reproducibility records scale with the strength of the conclusion.
