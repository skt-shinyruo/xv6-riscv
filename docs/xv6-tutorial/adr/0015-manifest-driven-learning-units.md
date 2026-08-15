# Drive Fixed Learning Units from a Central Manifest

Each standalone tutorial unit will use a fixed Markdown learning loop, while `curriculum.json` remains the sole authority for machine-readable status, prerequisites, source anchors, evidence dimensions, and resource paths. Branch deltas appear locally only when they change the unit's model, source path, or oracle; a small index may collect them without becoming a parallel course.

## Considered Options

- Let every unit choose its own structure and metadata format.
- Duplicate structured metadata in JSON and Markdown front matter.
- Put all branch differences in a separate comparison textbook.

## Consequences

The validator can enforce unit completeness without parsing prose. Authors must mark an inapplicable learning-loop section explicitly, and reader-facing summaries may repeat manifest facts in natural language without becoming a second machine authority.
