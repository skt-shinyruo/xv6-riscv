# Rehome the Tutorial Question Set

The existing source-question materials under `docs/questions/` will become part of the standalone tutorial under `docs/xv6-tutorial/questions/`, where they will be rewritten against the tutorial manifest, learning units, and capability ladder. The old path will retain a short compatibility notice or redirect entry during migration; the two locations will not be maintained as synchronized copies.

## Considered Options

- Leave `docs/questions/` as a separate learning system and write a second question set.
- Copy the question set and keep both paths synchronized.
- Delete the existing question materials and recreate them without a migration path.

## Consequences

Migration requires an internal-link audit and an explicit decision about which old entry points remain as compatibility stubs. Tutorial questions become independent content with their own release status, source anchors, and acceptance artifacts.
