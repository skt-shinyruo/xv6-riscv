# 虚拟内存问题

本组问题已迁移到教程的[虚拟内存问题](../xv6-tutorial/questions/virtual-memory.md)。
教程位置是唯一权威问题集；此处只保留兼容入口，不维护同步副本。

## 兼容映射

1. **VM-01..VM-09** 已迁移到教程问题 `VM-01..VM-09`。
2. **BOOT-06** 已合并到教程问题 `VM-06`，该题现在负责完整 trampoline/page-table contract。

## 源码入口

- [`kernel/memlayout.h`](../../kernel/memlayout.h)
- [`kernel/riscv.h`](../../kernel/riscv.h)
- [`kernel/kalloc.c`](../../kernel/kalloc.c)
- [`kernel/vm.c`](../../kernel/vm.c)
- [`kernel/trap.c`](../../kernel/trap.c)
- [`kernel/exec.c`](../../kernel/exec.c)
- [`kernel/proc.c`](../../kernel/proc.c)
