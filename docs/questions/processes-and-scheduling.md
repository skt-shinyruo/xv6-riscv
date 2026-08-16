# 进程与调度问题

本组问题关注进程状态发布、调度器与进程之间的锁所有权交接，以及 `fork/exec/exit/wait/sleep/wakeup/kill` 的并发语义。

## 问题

1. **PROC-01** 已迁移到[调度与同步问题 `SCHED-01`](../xv6-tutorial/questions/scheduling-and-synchronization.md#sched-01原-proc-01)。此处只保留兼容入口。
2. **PROC-02** 已迁移到[调度与同步问题 `SCHED-02`](../xv6-tutorial/questions/scheduling-and-synchronization.md#sched-02原-proc-02)。此处只保留兼容入口。
3. **PROC-03** 已迁移到[进程生命周期问题 `LIFE-02`](../xv6-tutorial/questions/process-lifecycle.md#life-02原-proc-03)。此处只保留兼容入口。
4. **PROC-04** 已迁移到[进程生命周期问题 `LIFE-03`](../xv6-tutorial/questions/process-lifecycle.md#life-03原-proc-04)。此处只保留兼容入口。
5. **PROC-05** 已迁移到[进程生命周期问题 `LIFE-04`](../xv6-tutorial/questions/process-lifecycle.md#life-04原-proc-05)。此处只保留兼容入口。
6. **PROC-06** 已迁移到[进程生命周期问题 `LIFE-05`](../xv6-tutorial/questions/process-lifecycle.md#life-05原-proc-06)。此处只保留兼容入口。
7. **PROC-07** 已迁移到[进程生命周期问题 `LIFE-06`](../xv6-tutorial/questions/process-lifecycle.md#life-06原-proc-07)。此处只保留兼容入口。
8. **PROC-08** 已迁移到[进程生命周期问题 `LIFE-07`](../xv6-tutorial/questions/process-lifecycle.md#life-07原-proc-08)。此处只保留兼容入口。
9. **PROC-09** 已迁移到[调度与同步问题 `SCHED-06`](../xv6-tutorial/questions/scheduling-and-synchronization.md#sched-06原-proc-09)。此处只保留兼容入口。
10. **PROC-10** 已迁移到[调度与同步问题 `SCHED-08`](../xv6-tutorial/questions/scheduling-and-synchronization.md#sched-08原-proc-10)。此处只保留兼容入口。
11. **PROC-11** 已迁移到[进程生命周期问题 `LIFE-08`](../xv6-tutorial/questions/process-lifecycle.md#life-08原-proc-11)。此处只保留兼容入口。
12. **PROC-12** 已迁移到[进程生命周期问题 `LIFE-09`](../xv6-tutorial/questions/process-lifecycle.md#life-09原-proc-12)。此处只保留兼容入口。

## 源码入口

- [`kernel/proc.h`](../../kernel/proc.h)
- [`kernel/proc.c`](../../kernel/proc.c)
- [`kernel/swtch.S`](../../kernel/swtch.S)
- [`kernel/sysproc.c`](../../kernel/sysproc.c)
- [`kernel/exec.c`](../../kernel/exec.c)
- [`user/sh.c`](../../user/sh.c)

## 配套文档

- [进程与调度](../xv6-riscv/kernel/processes-and-scheduling.md)
- [上下文切换](../xv6-riscv/assembly/context-switch.md)
- [`fork -> exec -> wait`](../xv6-riscv/flows/fork-exec-wait.md)
- [kill 一个阻塞进程](../xv6-riscv/flows/kill-blocked-process.md)
- [全局正确性不变量](../xv6-riscv/correctness/global-invariants.md)
