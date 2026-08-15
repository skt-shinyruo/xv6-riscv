# Provide Zero-Prerequisite Entry Through a Foundation Path

The documentation will accept learners without prior C, computer-organization, command-line, or debugging knowledge by providing a separate foundation path. The xv6 core path begins only after its explicit entry contract is met, rather than interrupting every kernel chapter to reteach general prerequisites.

## Considered Options

- Require C, data structures, and basic computer organization before entering the documentation.
- Teach prerequisite material inline whenever an xv6 chapter first needs it.

## Consequences

The foundation path and core path need separate learning outcomes and an explicit transition check. The gate requires a learner to build and debug a small C program using pointers, structures, bit operations, and a linked structure; operate the command-line build, QEMU, and cross-toolchain workflow; explain a simple C/RISC-V call stack and argument transfer; and submit a short GDB trace of registers, memory, and frames. Core chapters may link back to foundation units but must not duplicate their explanations.
