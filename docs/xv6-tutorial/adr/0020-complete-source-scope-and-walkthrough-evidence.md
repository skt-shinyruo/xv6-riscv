# Cover All Handwritten Inputs and Retain Walkthrough Evidence

A coverage-complete tutorial release will account for all handwritten kernel and user sources, assembly, headers, linker scripts, build and image tools, test drivers, debugging configuration, and generator inputs in its pinned baseline. Generated outputs are taught through their provenance and contracts. Official external specifications may support or deepen the tutorial, but the core path must remain usable without network access.

## Considered Options

- Limit coverage to kernel C and assembly or files reached by the main flows.
- Make external specifications required reading for core completion.
- Record pedagogical verification as an untraceable Boolean.

## Consequences

Each verified path also requires an anonymized non-author walkthrough record under `docs/xv6-tutorial/reviews/`, including entry capability, baseline, blockers, misconceptions, artifacts, and corrections. External-link availability is not a release gate.
