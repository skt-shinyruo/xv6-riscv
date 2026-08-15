# Use a Standard-Library Curriculum Manifest

The standalone tutorial will maintain one `docs/xv6-tutorial/curriculum.json` manifest with a schema version, stable semantic unit IDs, stage order, hard and non-blocking dependency edges, source coverage ownership, release status, and tutorial resource paths. A Python standard-library validator will check structural and lightweight source-anchor invariants without adding YAML, TOML, Markdown-parser, ctags, or Clang dependencies.

## Considered Options

- Encode curriculum metadata in YAML front matter or Markdown tables.
- Require TOML and establish a Python 3.11 runtime floor.
- Add language-specific parsers and a full Markdown validator immediately.

## Consequences

JSON is more verbose but portable and strict. The first validator can check that a source file exists and contains an anchor token, but cannot prove symbol definitions, call graphs, or Markdown heading-fragment semantics; those limitations must remain explicit.
