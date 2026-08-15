# Put the Tutorial in an Independent Directory

The curriculum will live as a standalone document set in `docs/xv6-tutorial/`, separate from the implementation reference in `docs/xv6-riscv/`. The two document sets are independently maintained and do not link to one another, while the tutorial may still identify repository source symbols, tests, commands, and runtime configuration directly.

**Status: accepted**

## Considered Options

- Add a curriculum index over the existing reference directory.
- Move the existing implementation documents into the tutorial directory.
- Maintain duplicated tutorial and reference directories.

## Consequences

The tutorial and reference are independent document sets: neither links to the other or automatically requires updates in the other, and both may independently explain the same xv6 mechanism. The tutorial needs its own navigation, terminology, source-tracing instructions, and maintenance checks. It may use repository source anchors, test names, commands, and configuration as evidence without making the reference directory a prerequisite.
