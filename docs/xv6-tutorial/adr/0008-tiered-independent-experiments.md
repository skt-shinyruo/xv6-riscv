# Build Independent Experiments in Three Evidence Tiers

The standalone tutorial will define its own observation exercises, bounded change labs, and evidence projects instead of linking or copying the independent reference labs. Tutorial resource bundles will live under `docs/xv6-tutorial/` and be applied explicitly to learner branches; the pinned executable baseline will not contain dormant instructional hooks by default.

## Considered Options

- Give every experiment one uniform difficulty and evidence contract.
- Copy the existing implementation-reference labs into the tutorial.
- Permanently add all instructional hooks and fixtures to the executable baseline.

## Consequences

Observation exercises must produce runtime or source-trace evidence without implementation changes. Bounded change labs must cover scoped behavior and regressions. Evidence projects may require learners to build stable fault identifiers, scheduling gates, or crash hooks, but must label those facilities as work to implement rather than capabilities already present.
