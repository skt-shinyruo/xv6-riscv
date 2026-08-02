# timer 抢占与跨 hart 恢复

每个 hart 在 `start()->timerinit()` 中独立把 `stimecmp` 设为 `time+1000000`。本文从一次 timer pending 追到进程让出 CPU，再说明同一进程如何可能由另一个 hart 恢复。

## 1. 两种入口

| 被打断位置 | 低层入口 | C 路径 |
|---|---|---|
| 用户态 | `uservec` 保存完整用户 GPR、换内核栈/页表 | `usertrap()` |
| 内核态 | `kernelvec` 在当前内核栈保存 caller-saved GPR 与 `gp`；`s0..s11` 由 C ABI 保持 | `kerneltrap()` |

两条路径均通过 `devintr()` 识别 `scause=supervisor timer interrupt`，调用 `clockintr()` 并返回 `which_dev=2`。

## 2. `clockintr()` 的时间语义

所有 hart 都重写自己的 `stimecmp = r_time()+1000000`，这同时清除当前 timer 请求。只有 hart 0 持 `tickslock` 执行 `ticks++` 和 `wakeup(&ticks)`；因此：

- `ticks` 是 hart 0 已处理的 tick 数，不是所有 hart timer 次数之和；
- 下一 deadline 基于 handler 当前时间，没有补发遗漏 tick；长时间关中断会令逻辑 tick 落后墙钟；
- 用户 `pause(n)` 等待的是 `ticks` 差值，粒度和延迟受 hart 0 调度/中断影响；内部 `sleep(&ticks, &tickslock)` 只是它的等待机制。

## 3. 从 trap 到 `yield()`

用户 trap 在处理设备、检查 `killed` 后，对 timer 调 `yield()`；内核 trap 仅在 `myproc()!=0` 时 yield。idle scheduler 的 timer 只更新 deadline/`ticks`，不会对不存在的进程调用 sched。

```text
yield
  acquire(p.lock)
  p.state = RUNNABLE
  sched() verifies: p.lock held, noff==1, interrupts off, state!=RUNNING
  swtch(&p.context, &cpu.context)
```

在切回 scheduler 时，两种入口留下的状态位置不同：用户入口的 GPR trapframe 位于独立的 `p->trapframe` 页，只有 `usertrap()/yield()` 的 C 栈帧留在进程内核栈；内核入口的 `kernelvec` 保存帧和 `kerneltrap()/yield()` 的 C 栈帧都留在该内核栈。`struct context` 只保存恢复这些 C continuation 所需的 callee-saved 集合。scheduler 恢复后清 `c->proc`，最后释放 `p->lock`，该槽才可由其他 scheduler 选取。

## 4. 为什么可以迁移

所有 hart 都线性扫描同一个 `proc[NPROC]`，没有 affinity、per-CPU run queue 或 work stealing。旧 hart 释放 `p->lock` 后，任意 hart 可取得它、观察 `RUNNABLE`、设置 `RUNNING` 和自己的 `c->proc=p`，再：

```text
swtch(&new_cpu.context, &p.context)
  -> 返回到旧 hart 上保存的 sched/yield continuation
```

context 不保存 `tp`，所以恢复后保留新 hart 的 id。若是内核 trap 帧，`kernelvec` 也刻意不恢复旧 `tp`。回到用户态前，`prepare_return()` 把新 `tp` 写入 `trapframe->kernel_hartid`；下一次 user trap 才会加载正确的新 hart 身份。

页表本身随进程对象，不随 CPU；返回用户态由 `userret` 写用户 `satp` 并 `sfence.vma`。当前系统没有 ASID，切换时做全局本地 TLB flush。也没有 IPI 协议来请求远端 TLB 或 instruction-cache 同步。

## 5. happens-before

```text
old hart holds p.lock
  -> state=RUNNABLE
  -> save p.context and enter scheduler
  -> old scheduler releases p.lock
  -> new hart acquires p.lock
  -> observes state and sets RUNNING/c.proc
  -> loads p.context
  -> process releases p.lock after returning from sched
```

`p->lock` 同时保护状态发布和“内核栈不可被两 hart 同时执行”。任何绕过锁的 run-queue 优化都必须重新建立这条顺序。

## 6. 无 reschedule IPI 的后果

把进程设为 `RUNNABLE` 不会唤醒指定的 idle hart。idle scheduler 在 `wfi` 中只能依赖其本地 timer 或设备中断返回。当前每 hart timer 给出了周期性机会，但不保证立即调度；若平台只给部分 hart timer、保留 `mstatus.TW` 使 `wfi` trap，或 timer 配置失败，活性会改变。

同样，kill/fork/wakeup 只发布状态。延迟上界不能只从临界区长度推导，还包括下一个能让 scheduler 扫描的本地事件。

## 7. 失败与边界

- timer 可在系统调用内核路径中触发；`kerneltrap` 保存/恢复入口 `sepc/sstatus`，否则调度期间的其他 trap 会覆盖它们。
- `yield` 不是强制公平：scheduler 从槽 0 开始扫描，一轮内可能运行多个进程，策略无时间配额统计。
- `ticks` 32 位回绕依赖无符号差值的有限等待用法；超长等待不在设计范围。
- 多 hart 同时 timer 会各自抢占，只有 hart 0 竞争 `tickslock` 更新全局 ticks。
- `sfence.vma` 不提供 `fence.i` 语义；进程迁移后的新代码可见性依赖当前平台/工具链假设。

## 8. 验证实验

1. 为每次 timer trace `hart,pid,entry(user/kernel),old state,new state`，并为每次恢复 trace hart；至少捕获一次 hart 变化。
2. 在 `state=RUNNABLE` 与 scheduler 释放 `p->lock` 之间设 checkpoint，证明第二 hart 不能提前加载该 context。
3. 让除 hart 0 外的 CPU 忙循环，核对它们会被本地 timer 抢占但不会增加 `ticks`。
4. 暂停 hart 0 一段受控虚拟时间，再恢复，验证没有 missed-tick catch-up；只观察，不把临时 hook 提交到正常内核。
5. 记录 wakeup 到首次运行延迟，分别比较目标 hart 在 scheduler、用户态和长内核临界区中的情形；不应硬编码为“恰好一个 tick”。
