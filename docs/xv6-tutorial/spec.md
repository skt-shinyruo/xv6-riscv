# xv6 教程建设规格

## Problem Statement

当前仓库已经有一套高精度的 xv6 实现参考文档，但它主要按子系统、控制流和正确性专题组织。它适合已经能够读 C、汇编、RISC-V 特权状态、锁和 GDB 的工程型读者，却没有一条面向 `Entry learner` 的可执行学习路径，也没有把“解释机制”“沿源码和运行时追踪”“进行有界修改”“评估证据边界”组织成可检查的能力阶梯。

用户希望把这些已有研究成果转化为一套循序渐进的教学文档，同时保留当前分支的真实实现差异、源码锚点、失败路径、资源所有权和验证边界。教程必须能独立演进，不应变成现有实现参考文档的同步副本，也不应把成功启动或一次测试通过误称为正确性证明。

问题还包括维护层面：如果章节顺序、硬前置、黑盒解除、源码覆盖、实验资源和非作者走读记录分散在 Markdown 中，后续多会话建设会重新引入重复解释、断链、错误前置和未经验证的完成标记。

## Solution

建设一套独立的 `Standalone tutorial`，以当前仓库分支作为 `Executable baseline`，通过中心 `Curriculum manifest` 管理阶段、学习单元、依赖、黑盒、解释所有权、源码覆盖、证据维度和资源。

教程从 `Foundation path` 开始，为没有 C、计算机组织、命令行和调试经验的 `Entry learner` 提供性能型入口；准备充分的 `Core learner` 也必须通过同一个 `Foundation gate` 或提交等价证据，然后加入同一张 `Shared learning graph`。核心路径采用七个 `Spiral pass`，每个 `Learning unit` 都以一个主要可观察成果和一个可独立审阅的出口产物结束。

内容按三种实验层级展开：不改变实现的 `Observation exercise`、范围受限的 `Bounded change lab`、要求确定性触发器和证据边界的 `Evidence project`。所有核心单元都包含观察任务；每个宏观阶段至少包含一个有界修改；核心完成还必须覆盖并发与等待、内存与资源所有权、持久化与恢复三个证据领域。

发布流程由一个标准库 Python 校验器和一个导航生成器保护。校验器验证清单结构、硬依赖 DAG、状态晋级、内部链接、源码 `path:symbol` 锚点、资源存在性、独立目录边界、基线源码内容和覆盖声明。导航由清单生成并提交，不能手工漂移。`verified` 还需要非作者走读记录；只有所有声明的源码范围都由 verified 单元负责时，版本才可标记为 `coverage-complete`。

## User Stories

1. As an `Entry learner`, I want a foundation path that assumes no prior C knowledge, so that I can enter xv6 without being blocked by unspoken prerequisites.
2. As an `Entry learner`, I want to practice pointers, structures, bit operations, arrays, and linked structures, so that kernel data structures are readable rather than mysterious.
3. As an `Entry learner`, I want to build a small C resource and diagnose its failures from the command line, so that I can distinguish source, object, executable, and running process.
4. As an `Entry learner`, I want a short call-stack and RISC-V register introduction, so that `pc`, `sp`, `ra`, argument registers, and return control are observable concepts.
5. As an `Entry learner`, I want guided GDB tasks for registers, memory, backtraces, and symbol breakpoints, so that debugging becomes a repeatable observation method.
6. As an `Entry learner`, I want a performance-based `Foundation gate`, so that entering the core path depends on demonstrated capability rather than a self-rating.
7. As a `Core learner`, I want to join the same learning graph after passing the gate, so that prepared learners are not forced into a separate curriculum with divergent explanations.
8. As a learner, I want to observe QEMU startup before learning every subsystem, so that the system has an executable shape from the beginning.
9. As a learner, I want early units to label unresolved mechanisms as black boxes, so that an incomplete model is explicit rather than silently wrong.
10. As a learner, I want to revisit startup and user execution in later spiral passes, so that new scheduler, memory, trap, and file-system knowledge changes an existing model.
11. As a learner, I want to understand how a user program becomes an ELF image and reaches its entry point, so that build rules, linker contracts, runtime startup, and shell behavior form one story.
12. As a learner, I want to distinguish a C function call from an xv6 system-call ABI, so that generated stubs, argument registers, trapframes, and return values are not conflated.
13. As a learner, I want to trace one system call from user code through `ecall`, trampoline, trap handling, dispatch, handler, and return, so that privilege boundaries are grounded in state transitions.
14. As a learner, I want stable symbol anchors instead of copied source listings or fragile line numbers, so that I can search the repository and retrace the lesson after unrelated edits.
15. As a learner, I want current-branch deviations from commonly cited xv6 behavior called out where they change the model, so that I do not apply an upstream assumption to this executable baseline.
16. As a learner, I want one small system-call lab that traverses declaration, generation, numbering, dispatch, handler, and test registration, so that I can make a bounded kernel-facing change.
17. As a learner, I want intermediate failure states in a lab, such as a deliberately unregistered dispatch entry, so that failure-path reasoning is practiced before the final feature passes.
18. As a learner, I want to trace process creation, scheduling, address spaces, lifecycle, and reclamation, so that fork, exec, wait, sleep, wakeup, and orphan handling share one ownership model.
19. As a learner, I want to investigate synchronization and waiting with deterministic event order, so that a timeout or a lucky run is not mistaken for a lost-wakeup result.
20. As a learner, I want to understand descriptors, pipes, console paths, and devices through resource ownership, so that I can explain both normal I/O and blocked progress.
21. As a learner, I want to follow file-system metadata, cache, log, disk, transaction, and recovery boundaries, so that persistence is not reduced to a list of system calls.
22. As a learner, I want crash and recovery tasks with explicit tear points and idempotence checks, so that SIGKILL, logical ordering, host persistence, and synthetic tearing are not conflated.
23. As a learner, I want to state the invariant before changing code, so that an implementation patch has a reviewable correctness claim.
24. As a learner, I want every experiment to state its oracle, allowed side effects, resource result, cleanup, and evidence limits, so that passing output is not the whole acceptance rule.
25. As a learner, I want to see evidence classified as `S`, `F`, `B`, `C`, or `R`, so that static reasoning, normal behavior, boundary behavior, concurrency, and recovery are evaluated distinctly.
26. As a learner, I want a reproducibility record scaled to the experiment, so that another person can rerun the same baseline, configuration, image, trigger, and expected result.
27. As a learner, I want to submit a source-and-runtime trace worksheet, so that observation work produces an artifact that another person can review.
28. As a learner, I want to submit a bounded-change report with focused, related, and full regressions, so that a small feature is evaluated beyond its happy path.
29. As an evidence-project learner, I want deterministic concurrency, fault, or crash triggers, so that a negative result is attributable to a known event rather than timing luck.
30. As an evidence-project learner, I want a resource ledger and cleanup proof, so that leaked pages, processes, files, inodes, buffers, log entries, or temporary images are visible.
31. As a learner, I want to explain what my evidence cannot establish, so that tests are not presented as formal proof of lock graphs, happens-before, DMA, or persistence guarantees.
32. As a learner, I want tutorial prose in Chinese while source names, commands, ABI terms, and specification names remain exact, so that the material is accessible without making repository navigation ambiguous.
33. As a learner on Linux or WSL, I want a command-line and QEMU-first environment, so that VSCode is optional and the core path remains reproducible without a particular editor.
34. As a tutorial maintainer, I want one explanation owner for each mechanism, so that later spiral passes add perspective without copying competing explanations.
35. As a tutorial maintainer, I want one manifest to define unit identity, order, hard prerequisites, related reading, black boxes, evidence dimensions, resources, and source ownership, so that sequencing is machine-checkable.
36. As a tutorial maintainer, I want stable semantic unit IDs independent of filenames and display order, so that reordering or restructuring does not break learner records.
37. As a tutorial maintainer, I want the validator to reject a verified unit whose hard prerequisites are not verified, so that publication status reflects a real path.
38. As a tutorial maintainer, I want the validator to reject stale generated navigation and broken internal links, so that the entry points remain trustworthy.
39. As a tutorial maintainer, I want the validator to reject forbidden links into the independent implementation-reference directory, so that the standalone tutorial remains independently navigable.
40. As a tutorial maintainer, I want the validator to compare taught source content with the pinned executable baseline, so that a documentation-only commit does not falsely look like a source release while a source drift cannot go unnoticed.
41. As a tutorial maintainer, I want generated outputs such as syscall assembly taught through their generator and contract, so that coverage does not require treating derived files as handwritten source owners.
42. As a tutorial maintainer, I want coverage ownership to include handwritten kernel and user inputs, assembly, headers, linker scripts, build and image tools, test drivers, debugging configuration, and generator inputs, so that the complete path reflects the executable baseline.
43. As a tutorial maintainer, I want existing source questions migrated only with their verified owning units, so that learners do not encounter questions whose prerequisites and explanations do not exist.
44. As a tutorial maintainer, I want the old question location to retain only a compatibility notice during migration, so that two question sets do not become synchronized copies.
45. As a tutorial maintainer, I want releases to use `0.x` while coverage and walkthroughs are incomplete, so that draft content is useful without implying course completion.
46. As a tutorial maintainer, I want `1.0.0` reserved for pinned-baseline coverage-complete release gates, so that the version communicates the strength of the learning path.
47. As a non-author reviewer, I want an anonymized walkthrough record with baseline, attempted path, artifacts, blockers, and corrections, so that `verified` means somebody else could actually use the unit.
48. As a project maintainer, I want the entire build to be decomposable into dependency-linked tickets, so that future sessions can work blockers-first with minimal manual coordination.
49. As a project maintainer, I want each ticket to have a bounded outcome, source scope, acceptance oracle, and release impact, so that an implementation agent can finish it without reopening accepted ADRs.
50. As a project maintainer, I want new irreversible decisions, permission needs, specification conflicts, and unverifiable assumptions to be the only automatic stopping conditions, so that routine editorial and implementation choices proceed without repeated intervention.

## Implementation Decisions

- The deliverable is a standalone tutorial document set. It may independently explain mechanisms also described elsewhere, but it has no documentation links or synchronization obligation with the implementation-reference set.
- The current repository branch is the `Executable baseline`. Each curriculum release pins a full commit and records compatibility information; changes to taught source require a deliberate release update.
- The initial release is `0.1.0`, remains `draft`, and is not `coverage-complete`. No unit may be marked `verified` without a non-author walkthrough record.
- The core audience model has two entry conditions: `Entry learner` uses the foundation path, while `Core learner` may pass the same `Foundation gate` through equivalent evidence. Both use one shared dependency graph after the gate.
- The foundation gate checks command-line and build operation, C pointers and structures, basic machine and RISC-V call-stack reasoning, and guided GDB observation of registers, memory, and backtraces.
- The core path has seven stages: observing the running system; programs, build products, ELF, user runtime, ABI, init, and shell; kernel boundaries through boot, traps, system calls, interrupts, and assembly; processes, scheduling, memory, lifecycle, and reclamation; communication and device I/O; file-system persistence, logging, transactions, and recovery; and evidence, invariants, resources, faults, traceability, scalability, and synthesis.
- A `Learning unit` follows a fixed Markdown loop: problem and outcomes; prerequisites and black boxes; minimal model and invariants; source trace; observation; bounded change; oracle, evidence, failure paths, and limits; exit artifact and next unit. A unit may explicitly declare a task `N/A` when the task belongs to another unit.
- Each unit has one primary observable outcome and one independently reviewable exit artifact. The capability ladder progresses from explanation to source/runtime tracing, bounded modification, and evidence-level mastery.
- Source navigation uses stable `path:symbol` anchors, repository search, call relationships, and runtime observations. Copied long listings and line-number-only references are not curriculum contracts.
- The manifest is the sole machine-readable authority for stable IDs, stage order, hard `requires` edges, non-blocking `related` edges, black boxes, resolutions, explanation ownership, source anchors, evidence dimensions, resources, reviews, release status, and source coverage.
- Hard prerequisites are blocking edges; related units are navigational context only. The `requires` graph must be acyclic. A verified unit may depend only on verified units.
- Each mechanism has one explanation owner within the standalone tutorial. Later spiral passes can add a new perspective or evidence without becoming duplicate full explanations.
- Source coverage includes handwritten kernel and user C, assembly, headers, linker scripts, build and image tools, test drivers, debugging configuration, and generator inputs. Generated outputs are covered through provenance and contracts rather than independent ownership.
- The first core pilot covers system observation, user program and ABI construction, one system-call round trip, and a bounded minimal-system-call lab. The remaining stages are represented as planned units until their content is written.
- Experiments have three contracts. Observation exercises change no implementation and produce a source/runtime trace. Bounded change labs require a scoped patch, invariants, normal and failure oracles, cleanup checks, and focused, related, and full regressions. Evidence projects add deterministic triggers, applicable evidence dimensions, resource accounting, isolated reproduction, recovery or idempotence checks where relevant, and explicit limitations.
- Every core unit includes an observation task. Every macro-stage includes at least one bounded change lab. The full curriculum must include evidence projects in concurrency and waiting, memory and resource ownership, and persistence and recovery.
- Evidence dimensions are static reasoning (`S`), normal function (`F`), boundary or failure behavior (`B`), concurrency (`C`), and recovery or persistence (`R`). A unit declares only the dimensions relevant to its claim.
- Correctness oracles connect an input and trigger to expected observable outcomes, state, allowed side effects, resource behavior, and recovery behavior. Timeout is a watchdog only, never a correctness oracle. Boot success and a passing general test suite are supplementary evidence.
- Deterministic event order, stable fault identifiers, isolated QEMU runs, temporary images, resource ledgers, exact panic or return observations, and idempotent recovery checks are required when the claimed evidence depends on them. Proposed fault-injection facilities must be labeled as proposed until implemented.
- Question materials migrate from the old question set only when their owning tutorial units become verified. The tutorial location becomes authoritative after migration; the old location retains only a compatibility notice and is not synchronized.
- Navigation is generated from the manifest and committed. The validator supports normal release checks and an explicit development mode that warns when taught source differs from the pinned baseline.
- The supported core environment is Linux or WSL, command line, repository toolchain, QEMU, and GDB. VSCode is an optional interface. Chinese is used for instructional prose; exact English symbols, commands, ABI terms, and specification names remain unchanged.
- The primary testing seam is the tutorial publication pipeline: manifest, validator, and navigation generator. Unit Markdown, resources, runtime observations, experiments, and non-author walkthroughs provide evidence at the learner-facing boundary without creating a parallel per-chapter harness.
- The implementation changes only tutorial documents, manifest metadata, resource bundles, templates, validation/generation tooling, and review records. The xv6 kernel, user programs, and host test implementation are not modified as part of this specification.

## Testing Decisions

- Tests must exercise external tutorial behavior and release contracts rather than assert incidental implementation details of the Python scripts. The important question is whether a learner or maintainer receives a correct path, artifact, failure signal, and evidence boundary.
- The highest reusable seam is the publication pipeline. A clean manifest must pass validation, generated navigation must be current, every non-planned resource must exist, every source anchor must resolve to a token in the pinned baseline, and forbidden cross-directory links must fail validation.
- The manifest validator must cover schema version, release status, baseline commit, stage uniqueness, unit identity, path uniqueness, hard and related edges, DAG acyclicity, black-box introduction and resolution, explanation ownership, evidence dimensions, resource existence, verified prerequisite status, source scope, baseline source content, and Markdown links.
- The navigation generator must be tested for deterministic output, planned-unit rendering, stage ordering, status counts, and stale-file detection. Running generation followed by `--check` is the release-level behavior.
- Foundation resources must be executable on the supported host environment. The pointer/list resource script is the first smoke seam; GDB command resources must remain readable and tied to the current baseline.
- Observation units are accepted only with a reviewable trace artifact that records source anchors, runtime state, transitions, and unresolved black boxes. A text summary without observable evidence is insufficient.
- Bounded change labs are accepted only with a scoped patch, invariant statement, normal oracle, boundary/failure oracle, cleanup result, focused test, related regression, full regression, and explicit limitations. The existing xv6 test driver and user-test conventions are prior art for runnable commands, while the tutorial adds the stronger oracle and cleanup contract.
- Evidence projects are accepted only with deterministic triggers, applicable `S/F/B/C/R` evidence, isolated reproduction, resource accounting, and recovery/idempotence checks when applicable. Random delay, stress, timeout, or a single SIGKILL result cannot substitute for a deterministic oracle.
- Concurrency evidence must use event ordering or a reproducible bad interleaving; final process exit is not enough. Memory evidence must account for ownership, rollback, and resource exhaustion where claimed. Persistence evidence must distinguish QEMU logical order, host persistence, and synthetic tear profiles.
- Non-author walkthroughs are part of the test contract. The record must identify the anonymized learner profile, pinned baseline, attempted path, artifacts, blockers or misconceptions, corrections, and resulting status change. `verified` without this record is invalid.
- A `coverage-complete` release is tested against the complete declared source scope and must have verified owners for every scoped handwritten input. Generated outputs are tested through their generator and ABI or contract checks.
- Existing implementation-reference traceability, user-test registration, fault-injection specifications, and recovery-oracle conventions are prior art for evidence vocabulary. They inform the standalone tutorial but are not linked runtime dependencies.
- Initial pilot verification has passed the manifest, navigation, JSON, source-anchor, internal-link, and foundation-resource checks. QEMU/GDB walkthroughs, actual learner submissions, and full evidence projects remain future release gates rather than being claimed as complete now.

## Out of Scope

- Rewriting, reorganizing, linking to, or synchronizing the existing implementation-reference document set.
- Treating the implementation-reference documents as a runtime or publication dependency of the standalone tutorial.
- Modifying xv6 kernel code, user programs, host build behavior, test behavior, or the executable baseline as part of tutorial construction.
- Promising parity with upstream xv6 or the xv6 book when the current branch intentionally differs. Branch deltas are taught only where they affect the learner's model.
- A fixed semester duration, fixed total study hours, or a timeboxed survey that leaves source areas permanently outside the coverage scope.
- Making VSCode, an online specification, or a particular graphical interface a prerequisite for the core path.
- Treating successful boot, one user test, full `usertests`, a zero exit status, or a timeout as a universal correctness proof.
- Building a general fault-injection framework during the initial pilot. Advanced projects may propose or implement such facilities under their own evidence contract.
- Migrating every existing question immediately. Migration is staged by verified owning unit, and the two question locations are never maintained as synchronized copies.
- Marking pilot units `verified` or the release `coverage-complete` before non-author walkthroughs, applicable evidence, and complete source ownership exist.
- Creating a separate bespoke test harness for every learning unit when the publication pipeline and existing executable seams can express the required contract.

## Further Notes

- The accepted decisions are recorded in the tutorial's ADR set and glossary. This specification treats them as constraints, not as prompts for a second design interview.
- The current pilot is deliberately narrow: it establishes the standalone directory, manifest, templates, resources, validator, generated navigation, foundation path, startup observation, user ABI explanation, system-call trace, and minimal system-call lab.
- The release baseline is the current executable commit recorded by the manifest. Documentation-only commits must remain valid because validation compares covered source content rather than requiring the repository `HEAD` to equal the baseline commit.
- Implementation should proceed as tracer-bullet tickets in manifest order and `requires` order. Foundation units block the first core slice; the first core slice blocks later process, I/O, persistence, and evidence stages.
- The first question migration boundary is after a pilot unit has passed a non-author walkthrough. Until then, the existing question set remains untouched.
- A future ticket may add a review record, move a unit from `draft` to `verified`, or expand source ownership, but it must not silently change the release baseline, evidence contract, independence boundary, or accepted audience model.
- No unresolved ambiguity currently blocks specification. The only automatic pause conditions for future implementation are a new irreversible decision, required external permission, a conflict between the specification and accepted ADRs, or an acceptance oracle that cannot be made deterministic.
