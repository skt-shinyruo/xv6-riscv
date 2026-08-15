# Anchor Source Tracing by Symbol and Version the Baseline

Curriculum source navigation will use `path:symbol` anchors, repository search, call relationships, and runtime observation rather than copied listings or line-number references. Each curriculum release will pin an executable-baseline commit and record relevant dirty state and runtime configuration for exercises, while compatibility notes describe the moving branch.

## Considered Options

- Follow the moving branch without recording a commit.
- Permanently freeze the curriculum at one commit.
- Depend on copied source or line-number links instead of symbol anchors.

## Consequences

Source anchors must be checked when symbols move or are renamed, and behavioral exercises must report their pinned baseline. A tutorial release remains valid for its pinned commit until a deliberate new release selects and revalidates a newer baseline; changes to source or the independent reference documents do not automatically trigger a tutorial update. Short excerpts remain acceptable only when instruction-level reading is itself the learning task.
