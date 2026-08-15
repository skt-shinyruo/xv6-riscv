# Generate Navigation and Migrate Questions by Verified Unit

Standard-library tooling will generate and commit tutorial navigation from `curriculum.json`, and validation will fail when generated files are stale. Existing source questions will move into `docs/xv6-tutorial/questions/` only as their owning tutorial units become verified; `docs/questions/` will retain a short compatibility entry rather than a synchronized copy.

## Considered Options

- Generate navigation only locally or maintain it manually.
- Move every question file before its tutorial prerequisites exist.
- Keep complete question sets at both old and new paths.

## Consequences

Generated navigation must be deterministic and marked as generated. Release validation checks source anchors against the manifest's pinned baseline commit and fails on a mismatched checkout unless an explicit development or migration mode is selected.
