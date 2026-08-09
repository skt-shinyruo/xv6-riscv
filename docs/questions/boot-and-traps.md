# 启动与陷阱问题

本组问题用于建立从 QEMU reset 到用户程序运行的控制流，并明确 RISC-V 硬件、汇编入口和 C 内核各自承担的职责。

## 问题

1. **BOOT-01** 从 QEMU reset 到 shell 提示符，中间切换了哪些特权级、栈、页表和 trap 入口？
2. **BOOT-02** `_entry` 在进入 C 代码前就用 hart id 计算栈地址；如果 `CPUS > NCPU`，为什么可能先破坏内存而不是干净报错？
3. **BOOT-03** 为什么其他 hart 必须等待 hart 0 完成初始化？只使用普通变量而没有内存栅栏会发生什么？
4. **BOOT-04** 本仓库为什么把 `fsinit()` 和 `kexec("/init")` 放在第一次 `forkret()`，而不是 `main()`？
5. **BOOT-05** 用户执行 `ecall` 时，硬件到底自动完成了什么？哪些工作必须由 `uservec` 完成？
6. **BOOT-06** 为什么 trampoline 必须在用户页表和内核页表中映射到同一个虚拟地址？
7. **BOOT-07** 为什么系统调用要执行 `epc += 4`，而 page fault 不能推进 `epc`？
8. **BOOT-08** `usertrap()` 为什么要先切换 `stvec`、保存 trap 原因，然后才能打开中断？
9. **BOOT-09** `trapframe` 和 `context` 分别保存什么？一个进程阻塞在系统调用中时，两者何时会同时有效？
10. **BOOT-10** 进程跨 hart 恢复后，为什么不能简单恢复旧的 `tp`？

## 源码入口

- [`kernel/entry.S`](../../kernel/entry.S)
- [`kernel/start.c`](../../kernel/start.c)
- [`kernel/main.c`](../../kernel/main.c)
- [`kernel/trampoline.S`](../../kernel/trampoline.S)
- [`kernel/kernelvec.S`](../../kernel/kernelvec.S)
- [`kernel/trap.c`](../../kernel/trap.c)
- [`kernel/proc.c`](../../kernel/proc.c)

## 配套文档

- [启动与初始化](../xv6-riscv/architecture/boot-and-init.md)
- [`entry.S` 与 `start()`](../xv6-riscv/assembly/entry-and-start.md)
- [trampoline](../xv6-riscv/assembly/trampoline.md)
- [内核 trap vector](../xv6-riscv/assembly/kernel-trap-vector.md)
- [一次系统调用往返](../xv6-riscv/flows/syscall-round-trip.md)
