# 启动与陷阱问题

本组问题用于建立从 QEMU reset 到用户程序运行的控制流，并明确 RISC-V 硬件、汇编入口和 C 内核各自承担的职责。

## 问题

1. **BOOT-01** 已迁移到[启动、陷阱、中断与汇编边界问题 `BOUNDARY-01`](../xv6-tutorial/questions/boot-traps-and-interrupts.md#boundary-01原-boot-01)。此处只保留兼容入口。
2. **BOOT-02** 已迁移到[启动、陷阱、中断与汇编边界问题 `BOUNDARY-02`](../xv6-tutorial/questions/boot-traps-and-interrupts.md#boundary-02原-boot-02)。此处只保留兼容入口。
3. **BOOT-03** 已迁移到[启动、陷阱、中断与汇编边界问题 `BOUNDARY-03`](../xv6-tutorial/questions/boot-traps-and-interrupts.md#boundary-03原-boot-03)。此处只保留兼容入口。
4. **BOOT-04** 已迁移到[进程生命周期问题 `LIFE-01`](../xv6-tutorial/questions/process-lifecycle.md#life-01原-boot-04)。此处只保留兼容入口。
5. **BOOT-05** 已迁移到[系统调用往返问题 `SYSCALL-01`](../xv6-tutorial/questions/syscall-roundtrip.md#syscall-01原-boot-05)。此处只保留兼容入口。
6. **BOOT-06** 为什么 trampoline 必须在用户页表和内核页表中映射到同一个虚拟地址？
7. **BOOT-07** 已迁移到[系统调用往返问题 `SYSCALL-02`](../xv6-tutorial/questions/syscall-roundtrip.md#syscall-02原-boot-07)。此处只保留兼容入口。
8. **BOOT-08** 已迁移到[系统调用往返问题 `SYSCALL-03`](../xv6-tutorial/questions/syscall-roundtrip.md#syscall-03原-boot-08)。此处只保留兼容入口。
9. **BOOT-09** 已迁移到[启动、陷阱、中断与汇编边界问题 `BOUNDARY-04`](../xv6-tutorial/questions/boot-traps-and-interrupts.md#boundary-04原-boot-09)。此处只保留兼容入口。
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
