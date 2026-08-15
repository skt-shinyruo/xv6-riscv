# xv6 Teaching Documentation

This context defines the shared language for turning the repository's implementation notes into a progressive xv6 learning resource.

## Language

**Entry learner**:
A learner who may enter the documentation without prior C, computer-organization, command-line, or debugging knowledge.
_Avoid_: beginner, novice (without stating the entry contract)

**Core learner**:
A learner who can read the C used by xv6, reason about a basic machine and call stack, operate the command-line toolchain, and use guided debugging, but has not yet studied an operating-system kernel in depth.
_Avoid_: primary learner, intermediate learner

**Foundation path**:
The prerequisite path that takes an entry learner to the explicit entry contract of the core path.
_Avoid_: remedial appendix, optional background

**Foundation gate**:
A performance-based transition check in which an entry learner demonstrates the C, machine-model, command-line, build, and guided-debugging skills required by the core path.
_Avoid_: prerequisite quiz, self-assessment

**Core path**:
The coverage-complete path through xv6 concepts, source, observation, modification, and verification after the foundation entry contract is met.
_Avoid_: main chapters, advanced path

**Capability ladder**:
The ordered learning progression from explaining a mechanism, to tracing it in source and runtime state, to changing it safely, and finally to evaluating it with invariants and bounded evidence.
_Avoid_: difficulty level, beginner-to-advanced scale

**Evidence-level mastery**:
The top of the capability ladder, where a learner can state invariants, identify failure paths, design deterministic oracles, and explain the limits of the resulting evidence.
_Avoid_: proven correct, tests pass

**Evidence dimension**:
One of the tutorial's explicit evidence classes: static reasoning (`S`), normal function (`F`), boundary or failure behavior (`B`), concurrency (`C`), or recovery and persistence (`R`).
_Avoid_: proof level, test tier

**Correctness oracle**:
A stated rule connecting an input and trigger to an expected observable outcome, state, allowed side effects, resource result, or recovery result; a timeout alone is never an oracle.
_Avoid_: green test, successful boot

**Reproducibility record**:
The environment and event information needed to rerun an exercise and compare its oracle, scaled to the exercise's evidence strength.
_Avoid_: test log, console transcript

**Spiral pass**:
One traversal of an observable end-to-end behavior that treats some mechanisms as explicit black boxes and revisits the behavior later with additional state, boundaries, failure paths, or evidence.
_Avoid_: repeated chapter, overview pass

**Learning unit**:
A bounded curriculum segment with one primary observable outcome and one independently reviewable exit artifact, organized around a problem, a minimal model, source and runtime tracing, an appropriate change, and the limits of its evidence.
_Avoid_: reference document, source file tour

**Shared learning graph**:
The one prerequisite graph used by both entry learners and prepared learners; entry learners traverse the foundation path, while prepared learners may pass the foundation gate and join the same core path.
_Avoid_: beginner track, fast track

**Explanation owner**:
The one learning unit inside the standalone tutorial that gives the complete explanation of a mechanism; later spiral passes add a new perspective and link internally instead of repeating it in full.
_Avoid_: first mention, reference owner

**Observation exercise**:
A learning task that changes no implementation and produces evidence of control flow, execution context, or state transitions through source and runtime observation.
_Avoid_: reading question, warm-up lab

**Bounded change lab**:
A learning task that makes a scoped implementation change and evaluates its normal, boundary, failure, cleanup, and regression behavior.
_Avoid_: coding exercise, feature task

**Evidence project**:
A multi-unit investigation or implementation that states invariants, constructs deterministic concurrency, fault, or crash oracles, accounts for resources, and explains the limits of its conclusions.
_Avoid_: advanced lab, capstone (without an evidence contract)

**Tutorial resource bundle**:
Starter patches, fixtures, scripts, and expected artifacts stored with the standalone tutorial and explicitly applied to a learner's experimental branch when needed.
_Avoid_: baseline test infrastructure, hidden harness

**Curriculum layer**:
The progressive learning path that supplies motivation, prerequisites, reading questions, checkpoints, and exercise sequencing.
_Avoid_: tutorial rewrite, chapter copy

**Standalone tutorial**:
The independently navigable document set under `docs/xv6-tutorial/`; it must not require links into the implementation reference directory to complete its learning path.
_Avoid_: curriculum overlay, reference index

**Independent document sets**:
The tutorial and implementation reference are separately owned document sets with no cross-directory documentation links or automatic maintenance dependency; each may explain the same xv6 mechanism and evolve on its own schedule.
_Avoid_: synchronized views, generated mirror

**Coverage-complete path**:
A curriculum path whose scope grows with the executable baseline instead of being limited by a semester or total-hour budget.
_Avoid_: fixed-length course, survey track

**Implementation reference set**:
The independently maintained implementation documentation under `docs/xv6-riscv/`; it has no authority or synchronization relationship with the standalone tutorial.
_Avoid_: reference layer, tutorial source of truth

**Executable baseline**:
The current repository branch whose source, build, tests, and runtime behavior define the implementation being taught.
_Avoid_: upstream xv6, canonical xv6 (when referring to this branch)

**Curriculum release**:
A published state of the curriculum tied to a specific executable-baseline commit and accompanied by compatibility information for the moving branch.
_Avoid_: latest documentation, current HEAD

**Curriculum manifest**:
The standalone tutorial's machine-readable JSON record of releases, stages, learning units, dependencies, source coverage, and resource bundles.
_Avoid_: table of contents, generated index

**Stable unit ID**:
The semantic identity of a learning unit, independent of its display order or filename numbering.
_Avoid_: chapter number, page number

**Hard prerequisite**:
A `requires` edge that must point to a verified unit before a unit can be verified or entered on the core path.
_Avoid_: related reading, suggested link

**Source coverage owner**:
The learning unit responsible for teaching a declared source area in a coverage-complete release.
_Avoid_: source mention, linked file

**Coverage scope**:
The handwritten kernel and user sources, assembly, headers, linker scripts, host build and image tools, test driver, debugging configuration, and generator inputs included by a curriculum release; generated outputs are taught through provenance and contracts rather than owned individually.
_Avoid_: kernel files, files on the main path

**Tutorial question set**:
The source-grounded, question-driven learning material owned by the standalone tutorial, including migrated questions and their capability-ladder outcomes.
_Avoid_: detached quiz bank, reference index

**Walkthrough record**:
An anonymized account of a non-author learner's entry capability, pinned baseline, attempted path, observed blockers or misconceptions, submitted artifacts, and resulting tutorial corrections.
_Avoid_: approval flag, testimonial

**Unit status**:
The declared publication state of a learning unit: `planned`, `draft`, or `verified`; a verified path may depend only on other verified units.
_Avoid_: done, ready

**Coverage-complete release**:
A curriculum release in which every source area in the pinned executable baseline is owned by a verified learning unit.
_Avoid_: published release, latest version

**Source anchor**:
A stable source location expressed as a repository path and symbol name rather than a line number or copied implementation.
_Avoid_: source line, pasted listing

**Branch delta**:
A deliberate difference between the executable baseline and commonly cited xv6 or xv6 book behavior, explained only where it affects the learner's model.
_Avoid_: fork quirk, implementation oddity
