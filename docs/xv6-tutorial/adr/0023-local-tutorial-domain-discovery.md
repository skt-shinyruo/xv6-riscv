# Discover Tutorial Domain Records Inside the Standalone Directory

**Status: accepted**

The repository's default single-context rule discovers domain records at the
repository root, while the accepted standalone-directory decision requires all
tutorial-related material to remain inside the tutorial directory. The
tutorial therefore uses a subtree `AGENTS.md` rule that points agents to its
local glossary and ADRs whenever they change tutorial curriculum, tooling, or
release state. This is a scoped discovery override for the standalone document
set, not a second repository-wide domain context.

## Considered Options

- Move the tutorial glossary and ADRs to the repository-wide locations.
- Change the repository-wide discovery convention for every project area.
- Keep the local records without an agent discovery rule.
