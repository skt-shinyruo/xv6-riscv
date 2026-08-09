# 进程与调度问题

本组问题关注进程状态发布、调度器与进程之间的锁所有权交接，以及 `fork/exec/exit/wait/sleep/wakeup/kill` 的并发语义。

## 问题

1. **PROC-01** scheduler 为什么持有 `p->lock` 跨越 `swtch()`，并让切换后的进程负责释放它？
2. **PROC-02** `sched()` 为什么要求中断关闭、`noff == 1`、只持有 `p->lock`，且状态不能是 `RUNNING`？
3. **PROC-03** 如果 `kfork()` 过早把子进程设为 `RUNNABLE`，另一个 hart 可能观察到哪些半初始化状态？
4. **PROC-04** `fork()` 为什么能返回两次？父子进程不同的返回值具体在哪里写入？
5. **PROC-05** 为什么 `exec()` 不创建新进程？它成功后哪些进程属性改变，哪些必须保留？
6. **PROC-06** `exit()` 为什么不能自己释放内核栈、trapframe 和进程槽，必须留下 `ZOMBIE` 给 `wait()`？
7. **PROC-07** 如果 `wait(status)` 的用户地址非法，为什么不能顺手回收已经找到的 zombie？
8. **PROC-08** `parent` 只是裸指针，进程槽又会复用；`wait_lock` 和 reparent 如何阻止 ABA 问题？
9. **PROC-09** 请证明 `sleep(chan, lk)` 不会丢失唤醒。为什么 waiter 和 producer 只使用相同 `chan` 仍然不够？
10. **PROC-10** `wakeup()` 为什么只把进程设为 `RUNNABLE`，而不直接把资源交给它？
11. **PROC-11** `kill()` 为什么不是立即终止？一个阻塞进程从被 kill 到最终退出要经过哪些检查点？
12. **PROC-12** 当前 `kill(0)` 为什么可能污染一个 `UNUSED` 槽？下一次 `allocproc()` 会出现什么现象？

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
