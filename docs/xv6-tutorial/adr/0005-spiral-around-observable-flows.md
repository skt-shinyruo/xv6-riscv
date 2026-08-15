# Revisit Observable Flows in Successive Spiral Passes

The core path will use successive spiral passes: learners first observe a deliberately reduced end-to-end behavior with unresolved mechanisms marked as black boxes, then revisit it after learning the required subsystems, state transitions, failure paths, and evidence practices. This avoids both a long bottom-up delay before xv6 does anything meaningful and the current reference route's demand that a new learner understand complex flows before their prerequisites.

## Considered Options

- Teach every prerequisite bottom-up before showing a complete system behavior.
- Present complete end-to-end flows first and explain their dependencies afterward.
- Follow source-directory or file order.

## Consequences

Every spiral pass must state what is newly explained, what remains a black box, and which later pass resolves it. Repeated visits may summarize earlier knowledge but must link to the standalone tutorial's explanation owner instead of duplicating the full explanation inside the tutorial.
