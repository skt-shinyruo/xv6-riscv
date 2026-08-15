# Organize the Core Path into Seven Spiral Stages

The core path will group bounded learning units into seven stages: observing the running system; understanding programs, build products, ELF, the user runtime, ABI, init, and shell; crossing kernel boundaries through boot, traps, system calls, interrupts, and assembly; running processes through synchronization, scheduling, memory, lifecycle, and reclamation; communication and I/O through descriptors, pipes, consoles, and devices; persistence through the file system, cache, log, disk, transactions, and recovery; and evidence through invariants, resource bounds, failure analysis, fault injection, traceability, scalability, and synthesis.

## Considered Options

- Retain the implementation-reference README's eleven-step route.
- Follow kernel subsystems or source-directory order.
- Make each stage a single monolithic chapter.

## Consequences

Stages set display order and learning intent, while individually reviewable units define prerequisites and artifacts. Earlier stages may expose explicit black boxes that later stages resolve; evidence practices begin early even though the final stage synthesizes them.
