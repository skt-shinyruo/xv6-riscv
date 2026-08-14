# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

## Conventions

- Create: `gh issue create --title "..." --body "..."`
- Read: `gh issue view <number> --comments`
- List: `gh issue list --state open --json number,title,body,labels,comments`
- Comment: `gh issue comment <number> --body "..."`
- Label: `gh issue edit <number> --add-label "..."` or `--remove-label "..."`
- Close: `gh issue close <number> --comment "..."`

Infer the repository from `git remote -v`; `gh` does this automatically inside the clone.

## Pull requests as a triage surface

**PRs as a request surface: no.**

## Skill operations

- “Publish to the issue tracker” means creating a GitHub issue.
- “Fetch the relevant ticket” means running `gh issue view <number> --comments`.

## Wayfinding operations

- A map is one issue labelled `wayfinder:map`.
- Child tickets use GitHub sub-issues, falling back to a task list and `Part of #<map>`.
- Blocking edges use GitHub native issue dependencies, falling back to `Blocked by: #<n>`.
- Claim a ticket with `gh issue edit <n> --add-assignee @me`.
- Resolve it by commenting with the answer and closing the issue.
