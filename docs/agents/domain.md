# Domain Docs

This repository uses a single domain context.

## Before exploring

Read:

- `CONTEXT.md` at the repository root, when present.
- Relevant ADRs under `docs/adr/`, when present.

If these files do not exist, proceed silently. `/domain-modeling` creates them lazily when terminology or decisions are resolved.

## Layout

```text
/
├── CONTEXT.md
└── docs/adr/
```

Use terminology defined in `CONTEXT.md`. If required terminology is absent, reconsider whether it belongs to the project or record the gap for `/domain-modeling`.

If proposed work conflicts with an ADR, identify the conflict explicitly rather than silently overriding the decision.
