# 一次系统调用如何往返

## 问题场景与本单元成果

用户程序调用 `getpid()` 时，没有普通 C 函数能够直接读取内核中的进程对象。它必须执行 `ecall`，跨越特权级、页表和栈边界，由内核分派 handler，再把结果恢复到用户寄存器。

本单元的出口产物是一份完整 `getpid` 往返工作表。每一步必须记录当前特权级、页表、栈、关键寄存器、trapframe 和下一跳。

## 前置单元与暂存黑盒

硬前置：[用户程序如何成为可运行镜像](user-program-and-abi.md)。相关基础：[用 GDB 观察寄存器、内存和栈](../foundation/guided-debugging.md)。

本单元解除 `syscall-kernel-entry`。trampoline 为什么能同时映射在用户和内核页表、TRAPFRAME 映射如何建立、TLB 刷新和完整返回安全性仍记为 `full-trampoline-page-table-contract`，由进程与内存阶段解除。

## 最小模型和关键不变量

`getpid()` 没有参数，系统调用号是 `SYS_getpid`。完整路径：

```text
user getpid stub
  -> a7 = SYS_getpid
  -> ecall
  -> stvec 指向 trampoline:uservec
  -> 保存用户寄存器到 TRAPFRAME
  -> 切换内核栈、tp 和 satp
  -> usertrap
  -> syscall
  -> syscalls[SYS_getpid]
  -> sys_getpid
  -> trapframe->a0 = pid
  -> prepare_return
  -> trampoline:userret
  -> 恢复用户寄存器和 a0
  -> sret
  -> stub 的 ret
```

进入 `uservec` 时已经是 supervisor mode，但仍使用用户页表和用户栈；它必须先把用户现场保存到固定 TRAPFRAME 映射，才能安全切换到内核页表和内核栈。

关键不变量：

- `a7` 中的编号在保存现场后仍能从 `p->trapframe->a7` 读取。
- 用户 `a0` 先保存，handler 返回值最后写回同一个 trapframe 字段。
- `usertrap` 把保存的 `epc` 增加 4，返回时不能再次执行同一条 `ecall`。
- 切换 `satp` 前后的 trampoline 指令必须保持可执行。
- `syscall()` 只调用表中存在的编号；未知编号返回 `-1` 并打印诊断。

## 源码追踪计划

1. `user/usys.pl:entry`：生成 `getpid` stub。
2. `kernel/syscall.h:SYS_getpid`：编号。
3. `kernel/trampoline.S:uservec`：保存寄存器、切换栈和页表。
4. `kernel/trap.c:usertrap`：识别 `scause == 8`、推进 `epc`。
5. `kernel/syscall.c:syscall`：读取 `trapframe->a7` 并分派。
6. `kernel/sysproc.c:sys_getpid`：读取当前进程 pid。
7. `kernel/trap.c:prepare_return` 和 `kernel/trampoline.S:userret`：准备并恢复用户现场。

## 观察任务

启动 QEMU GDB，在 `syscall` 和 `sys_getpid` 设置断点。让 xv6 shell 运行：

```text
usertests reparent
```

该测试开始时调用一次 `getpid()`。在断点处记录 backtrace、`sepc/scause/sstatus/satp`、当前 hart，以及当前进程 trapframe 中的 `a0/a7/epc`。若调试器不能安全求值函数调用，不要在断点里调用 `myproc()`；通过调试信息和当前 CPU 的 `proc` 指针定位对象。

继续到 `sys_getpid`，记录返回值，再在下一次 `syscall` 断点前结束定向观察，避免把测试的后续大量系统调用混入同一工作表。

## 有界修改任务

`N/A`。本阶段的有界修改集中在独立的 [新增最小系统调用](../experiments/add-system-call.md) 实验，避免在追踪单元和实验规格中维护两份 patch 流程。

## Oracle、证据、失败路径和局限

- `S`：工作表覆盖从用户 stub 到 stub `ret` 的每个边界，并区分用户栈、内核栈和 trapframe。
- `F`：分派前 `trapframe->a7 == SYS_getpid`；handler 结果等于当前 pid；返回用户态后 `a0` 携带同一值。
- `B`：未知正编号不越界调用函数，而是返回 `-1` 并打印 `unknown sys call`。该行为在实验中用受控中间状态观察。
- 本单元没有证明 trampoline 映射、TLB 或并发迁移的完整正确性；断点还会改变时序。

## 退出产物与后续单元

提交 `getpid` 往返工作表、断点观察和证据局限说明。随后完成 [有界实验：新增最小系统调用](../experiments/add-system-call.md)。`full-trampoline-page-table-contract` 将在计划中的进程与内存单元解除。
