# Validate Internal Ownership and Publish Incrementally

Within the standalone tutorial, each mechanism will have one explanation owner and later spiral passes will link internally rather than repeat the full explanation. Tutorial releases may publish incrementally with units marked `planned`, `draft`, or `verified`; a release is `coverage-complete` only when every source area in its pinned baseline is owned by a verified unit.

## Considered Options

- Let each chapter repeat mechanisms as needed for local self-containment.
- Wait for complete source coverage before publishing any tutorial release.
- Track readiness informally without machine-readable status.

## Consequences

A repository validator must check internal links, dependency cycles, source anchors, tutorial resource bundles, required unit fields, the absence of links to `docs/xv6-riscv/`, verified-only prerequisite paths, and the source-coverage claim of any coverage-complete release.
