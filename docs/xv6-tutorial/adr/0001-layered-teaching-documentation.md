# Layer the Curriculum Over the Implementation Reference

**Status: superseded by ADR 0007**

The xv6 teaching resource will add a curriculum layer over the existing implementation reference layer instead of rewriting or duplicating it. The current repository branch is the executable baseline; the curriculum introduces branch deltas where they affect learning, while detailed mechanisms, contracts, invariants, evidence, and advanced lab oracles remain owned by the reference documents. This preserves the repository's existing audit precision while making a progressive path possible.

## Considered Options

- Rewrite the existing documents into a linear textbook.
- Maintain separate tutorial and reference copies with intentional duplication.

## Consequences

Curriculum chapters must link to canonical reference owners and may not restate their complete mechanisms. Changes to the implementation require updating the reference owner and the curriculum navigation or prerequisite links that depend on it.
