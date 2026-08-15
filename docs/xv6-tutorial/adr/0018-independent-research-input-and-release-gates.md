# Reuse Research Once and Verify Tutorial Releases Independently

Authors may use `docs/xv6-riscv/` as research input while independently rewriting the standalone tutorial; the resulting tutorial does not link to that directory or acquire a synchronization obligation. An incremental tutorial release becomes verified only after its manifest, generated navigation, internal links, forbidden-link rule, dependency status, pinned source anchors, resource bundles, and applicable experiment evidence pass their checks.

## Considered Options

- Ignore the existing implementation research and rediscover every fact from source.
- Copy the implementation documents while preserving their structure.
- Treat a complete source-coverage claim or a passing xv6 regression as the only release gate.

## Consequences

Reference-document changes do not invalidate a tutorial release; its pinned source baseline and own evidence do. Question migration completes only when each group is owned by a verified tutorial unit and the old path contains compatibility guidance rather than full duplicate content.
