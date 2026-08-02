#!/usr/bin/env bash

set -u

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
docs_dir="$repo_root/docs"
errors=0
checks=0

fail()
{
  printf 'docs-check: ERROR: %s\n' "$*" >&2
  errors=$((errors + 1))
}

check_file()
{
  checks=$((checks + 1))
  if [[ ! -f "$repo_root/$1" ]]; then
    fail "missing required file: $1"
  fi
}

traceability_matrix_contains_literal()
{
  local literal=$1
  awk '
    /^## 10\./ { exit }
    { print }
  ' "$docs_dir/reference/source-test-traceability.md" |
    grep -F -q -- "$literal"
}

required_docs=(
  .github/workflows/docs.yml
  docs/README.md
  docs/analysis/scalability.md
  docs/architecture/boot-and-init.md
  docs/architecture/repository-delta.md
  docs/architecture/trust-and-failure-model.md
  docs/assembly/README.md
  docs/assembly/context-switch.md
  docs/assembly/entry-and-start.md
  docs/assembly/kernel-trap-vector.md
  docs/assembly/trampoline.md
  docs/correctness/global-invariants.md
  docs/filesystem/filesystem-consistency.md
  docs/flows/console-keystroke-to-shell.md
  docs/flows/kill-blocked-process.md
  docs/flows/pipeline-fd-topology.md
  docs/flows/timer-preemption-and-migration.md
  docs/flows/unlink-crash-orphan-recovery.md
  docs/kernel/call-context-contracts.md
  docs/kernel/files-and-pipes.md
  docs/kernel/filesystem.md
  docs/kernel/memory.md
  docs/kernel/processes-and-scheduling.md
  docs/kernel/resource-bounds.md
  docs/kernel/storage-stack.md
  docs/kernel/synchronization.md
  docs/kernel/system-calls.md
  docs/kernel/traps-and-interrupts.md
  docs/labs/README.md
  docs/labs/add-system-call.md
  docs/labs/buffer-cache.md
  docs/labs/copy-on-write.md
  docs/labs/lazy-nx.md
  docs/labs/lost-wakeup.md
  docs/labs/offline-fsck.md
  docs/reference/resource-failure-matrix.md
  docs/reference/source-test-traceability.md
  docs/user/programs-and-tests.md
  docs/verification/fault-injection.md
)

for path in "${required_docs[@]}"; do
  check_file "$path"
done

# Check only simple, single-line relative Markdown targets ending in .md.  The
# fragment is intentionally ignored: renderer-specific anchor rules are not a
# reliable property for this local checker.
while IFS= read -r -d '' markdown; do
  while IFS= read -r token; do
    target=${token#](}
    target=${target%)}
    target=${target#<}
    target=${target%>}
    target=${target%%#*}

    case "$target" in
      ''|/*|*://*|mailto:*|data:*)
        continue
        ;;
    esac

    checks=$((checks + 1))
    if [[ ! -f "$(dirname -- "$markdown")/$target" ]]; then
      fail "broken Markdown target in ${markdown#"$repo_root/"}: $target"
    fi
  done < <(
    awk '
      /^[[:space:]]*```/ { fenced = !fenced; next }
      !fenced { print }
    ' "$markdown" |
      grep -oE '\]\(<?[^)>[:space:]]+\.md(#[^)>[:space:]]*)?>?\)' || true
  )
done < <(find "$docs_dir" -type f -name '*.md' -print0)

# Every handwritten implementation input must be named by its repository path
# somewhere in the documentation.  user/usys.S and all build artifacts are
# deliberately excluded because they are generated.
source_files=(Makefile test-xv6.py)
while IFS= read -r path; do
  source_files+=("${path#"$repo_root/"}")
done < <(
  find "$repo_root/kernel" -maxdepth 1 -type f \
    \( -name '*.c' -o -name '*.h' -o -name '*.S' -o -name '*.ld' \) -print
  find "$repo_root/user" -maxdepth 1 -type f \
    \( -name '*.c' -o -name '*.h' -o -name '*.ld' -o -name '*.pl' \) -print
  find "$repo_root/mkfs" -maxdepth 1 -type f -name '*.c' -print
)

while IFS= read -r source; do
  checks=$((checks + 1))
  if ! traceability_matrix_contains_literal "$source"; then
    fail "handwritten source has no row in the traceability matrix: $source"
  fi
done < <(printf '%s\n' "${source_files[@]}" | sort -u)

# Names declared in syscall.h must propagate through the generated-stub source,
# the kernel dispatch table, the public declaration file, and the ABI document.
syscall_names=()
while IFS= read -r name; do
  syscall_names+=("$name")
done < <(sed -nE 's/^[[:space:]]*#define[[:space:]]+SYS_([[:alnum:]_]+)[[:space:]]+[0-9]+.*$/\1/p' "$repo_root/kernel/syscall.h")

if ((${#syscall_names[@]} == 0)); then
  fail 'could not extract any syscall names from kernel/syscall.h'
fi

for name in "${syscall_names[@]}"; do
  checks=$((checks + 4))
  grep -F -q -- "entry(\"$name\")" "$repo_root/user/usys.pl" ||
    fail "SYS_$name has no user/usys.pl entry"
  grep -E -q -- "\[SYS_${name}\][[:space:]]+sys_${name}" "$repo_root/kernel/syscall.c" ||
    fail "SYS_$name has no kernel/syscall.c dispatch entry"
  grep -E -q -- "(^|[^[:alnum:]_])${name}[[:space:]]*\(" "$repo_root/user/user.h" ||
    fail "SYS_$name has no user/user.h declaration"
  grep -F -q -- "\`$name\`" "$repo_root/docs/kernel/system-calls.md" ||
    fail "SYS_$name is not named in docs/kernel/system-calls.md"
done

# Extract only registered test strings.  Functions that happen to look like
# tests but are absent from quicktests/slowtests are outside this file-level
# registration check.
registered_tests=()
while IFS= read -r name; do
  registered_tests+=("$name")
done < <(sed -nE 's/^[[:space:]]*\{[[:alnum:]_]+,[[:space:]]*"([^"]+)"\},[[:space:]]*$/\1/p' "$repo_root/user/usertests.c")

if ((${#registered_tests[@]} == 0)); then
  fail 'could not extract registered tests from user/usertests.c'
fi

for name in "${registered_tests[@]}"; do
  checks=$((checks + 1))
  if ! grep -F -q -- "\`$name\`" "$docs_dir/user/programs-and-tests.md"; then
    fail "registered usertest has no oracle entry in user/programs-and-tests.md: $name"
  fi
done

if ((errors != 0)); then
  printf 'docs-check: FAILED (%d errors, %d checks)\n' "$errors" "$checks" >&2
  exit 1
fi

printf 'docs-check: OK (%d checks)\n' "$checks"
