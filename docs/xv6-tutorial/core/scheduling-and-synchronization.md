# 调度、同步与等待

## 问题场景与本单元成果

一个进程在空 pipe 上等待时，timer 仍在到达，另一个 hart 可能修改 pipe，进程
也可能在醒来后换到另一 hart。若只把这些动作写成“sleep 后 wakeup”，就无法
回答三个关键问题：谁在 context switch 两侧拥有 `p->lock`，producer 的写入为何
不会落进检查与睡眠之间，以及恢复后的 `tp`/中断嵌套为何仍指向当前 hart。

本单元的唯一出口是一个可独立复核的“调度、同步与 lost-wakeup 证据报告包”。
报告同时包含 scheduler/锁/中断静态图、正确交接轨迹、确定性错误交错、主动
bad-state 快照、跨 hart 恢复、回归和完整资源清理。这些分节是一个出口产物，
不是若干互相独立的日志。

## 前置单元与暂存黑盒

硬前置：[进程生命周期与回收](process-and-memory.md)。相关边界来自
[启动、陷阱、中断与汇编边界](boot-traps-and-interrupts.md)：那里已经观察 timer
进入 `yield()`，本单元继续解释它怎样交给 scheduler，以及进程如何在另一 hart
恢复。

本单元完整拥有 scheduler selection、kernel context、spinlock、嵌套中断状态、
sleep channel、wakeup 与 lost-wakeup happens-before。以下机制继续作为显式黑盒：

- 页表、TLB、lazy fault 和用户映像权限由后续虚拟内存单元
  解释；这里的 `swtch()` 不切页表。
- pipe 字节账本、console 和设备完成路径由通信与 I/O 单元解释；这里仅把 pipe
  当成遵守条件等待协议的实例。
- 公平性、所有可能交错和形式化证明不由一次受控实验给出。

## 最小模型和关键不变量

### 两条 continuation 共享一把进程锁

`kernel/proc.c:scheduler()` 是每 hart 一条永不返回的 kernel continuation；每个
进程的 `struct context` 又保存一条可暂停的 kernel continuation。`kernel/swtch.S:swtch`
只保存/恢复 `ra`、`sp` 和 `s0-s11`。C ABI 允许调用破坏 caller-saved 寄存器；
跨调用仍活跃的值由编译器移入 callee-saved 寄存器、重新计算或 spill 到当前
kernel stack。`satp`、特权级和用户 trapframe 都不是这次切换的职责。

切换两侧的锁所有权不是普通函数调用所有权：

```text
scheduler: acquire(p->lock) -> RUNNABLE→RUNNING -> swtch(scheduler, p)
process:   从 forkret 或 sched 返回，接住 p->lock -> release(p->lock)

process:   acquire(p->lock) -> RUNNING→RUNNABLE/SLEEPING/ZOMBIE -> sched -> swtch(p, scheduler)
scheduler: 从 swtch 返回，接住 p->lock -> clear c->proc -> release(p->lock)
```

因此 `p->state`、`p->chan` 与“该进程正在哪个 CPU 运行”在交接期间没有无锁窗口。
如果本轮扫描没有找到 `RUNNABLE`，scheduler 会执行 `wfi`；它在下一轮先短暂
`intr_on()` 允许唤醒中断，再 `intr_off()` 固定扫描与 `wfi` 之间的窗口，避免
“所有进程都在等待”时既睡死又在扫描期间竞态。进程切回 origin scheduler 前
取得 `p->lock`，使 origin CPU 的 `noff` 从 0 到 1；scheduler 接住后 release 回
到 0。destination scheduler 后来 acquire 同一把锁时，其 CPU 的 `noff` 独立从
0 到 1；进程 continuation 在该 hart 接住并 release 后再回到 0。`noff` 不属于
被迁移的进程。
`sched()` 要求当前持有 `p->lock`、`noff==1`、中断关闭且 state 已不再是
`RUNNING`；这也排除了“还持有另一把 spinlock 就调度”的路径。第一次调度由
`forkret()` 释放 scheduler 交来的锁；之后 `yield()`、`sleep()` 和 `kexit()`
各自在自己的状态发布点进入 `sched()`。

### spinlock 同时约束内存顺序与本 hart 中断

`kernel/spinlock.c:acquire()` 先 `push_off()`，再以 `__ATOMIC_ACQUIRE` 原子交换
取得锁；`release()` 以 `__ATOMIC_RELEASE` 发布临界区写入，再 `pop_off()`。
acquire/release 让不同 hart 经同一锁观察临界区的 happens-before；关闭中断只
防止当前 hart 在持锁时被会重取同锁的 handler 打断，并不阻止其他 hart 并行。

`kernel/spinlock.h:struct spinlock` 保存 `locked` 以及仅用于调试的 `name/cpu` owner；
`kernel/sleeplock.h:struct sleeplock` 则把短时 spinlock、可睡眠的 `locked` 谓词和
持有者 pid 放在同一对象。header 给出可观察状态，真正的 acquire/release 与等待协议仍由
对应 `.c` 实现；不能看到 `pid` 就把 sleeplock 当作进程资源所有权。

`push_off()/pop_off()` 用 `struct cpu.noff/intena` 嵌套记录第一次关闭前的状态。
`mycpu()` 只能在中断关闭时使用，因为可抢占进程可能换 hart。`sched()` 中的
`intena` 是 C 局部状态，属于暂停的进程 kernel continuation；编译器
可把它放在 `swtch()` 保存的 callee-saved context 或当前 kernel stack。恢复后再
写入实际接住 continuation 的 CPU；源码注释也指出这个逻辑属性理想上属于
kernel thread，而不是永久属于某个 hart。

`tp` 更不能随进程 context 恢复旧值：它标识当前 hart，故不在 `struct context`
和 `swtch` 保存集合中。进程在新 hart 恢复后，`prepare_return()` 把当前 `r_tp()`
写入 trapframe 的 `kernel_hartid`；下一次 user trap 才能在 trampoline 中加载
正确 hart id。

`kernelvec` 进入 kernel-mode trap 时同样不恢复 `tp`；它保存 caller-saved 寄存器
后返回当前 kernel continuation，因而同一 migration 规则适用于 kernel trap。

### 条件锁与 p->lock 共同封闭 lost-wakeup 窗口

等待谓词不能只靠相同 `chan`。正确协议必须同时满足：

```text
waiter:   acquire(lk) -> while (!ready) sleep(chan, lk) -> recheck -> consume
producer: acquire(lk) -> ready=1 -> wakeup(chan) -> release(lk)
```

`sleep(chan, lk)` 在仍持有 `lk` 时先取得 waiter 的 `p->lock`，再释放 `lk`，发布
`chan/state=SLEEPING` 并调用 `sched()`。producer 取得 `lk` 后，即使在 waiter
发布 state 前写入 `ready`，它的 `wakeup()` 也必须再取得同一 `p->lock`；这把锁
会把 wake scan 排到睡眠发布之后。反之，producer 若先取得 `lk`，waiter 会在
重新检查谓词时看到 `ready=1`，根本不再睡眠。

关闭 check-to-sleep 窗口依赖 producer 持同一 `lk` 修改谓词并 wake，以及
`sleep()` 用 `p->lock` 接住 `lk`；只共享 channel 或无锁写谓词都不够。`while`
负责另一条不变量：waiter 醒来并重新取得 `lk` 后必须复查谓词。把它改成 `if`
不会在锁交接正确时重新打开该窗口，却会让 broadcast、竞争者先消费或取消 wake
后的 waiter 在假谓词下继续。`wakeup()` 只把匹配进程改为 `RUNNABLE`；它不交付
资源、不保证立刻运行，也可能同时唤醒多个 waiter。

spinlock 临界区本身不能 sleep。唯一合法的交接是把保护谓词的那把 `lk` 传给
`sleep()`；它先用 `p->lock` 接住状态，再释放 `lk`。调用者若还持有其他
spinlock，`sched()` 的 `noff==1` 会拒绝调度。`sleeplock` 能等待，是因为它只在
短暂持有内部 spinlock 时检查状态，并由 `sleep()` 在阻塞前释放该内部锁。中断
handler 不能保证存在可 sleep 的当前进程，还可能正好打断锁持有者；因此不能
调用会经 `myproc()` 进入 `sleep()` 的 sleeplock acquire。

### 抢占、单 hart 与多 hart

timer 在 `usertrap()` 或有当前进程的 `kerneltrap()` 中令 `which_dev==2`，随后
调用 `yield()`：在 `p->lock` 下把 `RUNNING` 发布为 `RUNNABLE`，再交给当前 hart
scheduler。任意 hart 的 scheduler 都能取得该 `p->lock` 并选择它，所以恢复
位置不是进程身份的一部分。

`CPUS=1` 足以观察 context switch 和条件等待，但若 gate 在关中断状态下自旋，
它会占住唯一 hart，不能用来控制另一参与者。本单元的确定性并发项目固定使用
`CPUS=2`，先让 producer 占住一 hart，再让 waiter 在另一 hart 到达 checkpoint；
专用 scheduler gate 还要求 origin hart 明确跳过已经 `RUNNABLE` 的 waiter，才
允许另一 hart 选择它。默认未 arm 时所有 hook 都立即返回；完整回归另以
`CPUS=1` 验证生产路径不依赖该 gate。

## 源码追踪计划

用稳定 `path:symbol` 建立 context、锁和等待三张图：

```sh
rg -n '^struct context|^struct cpu|^enum procstate' kernel/proc.h
rg -n '^swtch:' kernel/swtch.S
rg -n '^kernelvec:' kernel/kernelvec.S
rg -n '^scheduler\(void\)|^sched\(void\)|^yield\(void\)' kernel/proc.c
rg -n '^sleep\(void|^wakeup\(void' kernel/proc.c
rg -n '^struct spinlock|^struct sleeplock' kernel/spinlock.h kernel/sleeplock.h
rg -n '^acquire\(struct|^release\(struct|^push_off\(void\)|^pop_off\(void\)' kernel/spinlock.c
rg -n '^usertrap\(void\)|^kerneltrap\(\)|yield\(\)' kernel/trap.c
rg -n '^piperead\(|^pipewrite\(' kernel/pipe.c
rg -n '^bread\(' kernel/bio.c
rg -n '^begin_op\(' kernel/log.c
rg -n '^acquiresleep\(|^releasesleep\(' kernel/sleeplock.c
rg -n '^preempt\(' user/usertests.c
```

静态图至少包含以下边：

```text
timer -> yield -> sched -> swtch -> per-hart scheduler -> swtch -> continuation
waiter: lk -> p.lock -> release(lk) -> SLEEPING -> sched
producer: lk -> ready=1 -> p.lock in wakeup -> RUNNABLE
resume: scheduler owns p.lock -> sched returns -> release p.lock -> reacquire lk
```

不要用源码行号或一次构建的绝对地址代替这些 symbol 和关系。

## 观察任务

先区分 pinned 源码基线与走查时教程提交，再运行静态门：

```sh
rg -n '"baseline_commit":' docs/xv6-tutorial/curriculum.json
git rev-parse HEAD
python3 docs/xv6-tutorial/resources/scheduling-and-synchronization/run-lab.py --static-only
```

静态门从 pinned baseline 导出临时源码，验证 `swtch` 保存集合、scheduler 锁
交接、`sched` 前置、spinlock memory order、嵌套中断、sleep/wakeup、pipe 与
sleeplock 调用协议，以及 tutorial patch 的 11-path scope。它还构建临时
kernel、`schedtrace` 和私有镜像，并在逆向 patch 后比较完整源码快照。

完整报告命令为：

```sh
python3 docs/xv6-tutorial/resources/scheduling-and-synchronization/run-lab.py \
  --report /tmp/scheduling-and-synchronization-report.md
```

runner 在同一私有 `CPUS=2` QEMU 中先执行 `usertests preempt/pipe1/killstatus`，
再执行 fixed/broken/fixed/broken 四个连续 generation。host 逐字段解析 guest
事件，不接受 guest 自报 `PASS` 作为唯一判据。之后 quick 使用 `CPUS=2`，完整
`usertests` 使用 `CPUS=1`；每个 driver/QEMU 都是独立进程组。

## 有界修改任务

资源 [`syncproject.patch`](../resources/scheduling-and-synchronization/syncproject.patch)
和 [`run-lab.py`](../resources/scheduling-and-synchronization/run-lab.py) 是只应用于
临时导出的 tutorial audit seam，不修改 pinned baseline。patch 添加一个多路
`syncproject` syscall、固定 32 槽事件缓冲和 `schedtrace` guest；hook 必须同时
匹配 active generation、target pid 与专用 channel，不能全局破坏 `sleep()`。

这份完整 fixture 只服务 publication/non-author 的可重复验收，不是生产修复或
学习者可提交的 authoritative answer patch。学习者按[项目 rubric](../resources/scheduling-and-synchronization/rubric.md)
独立提交模型与证据报告，不复制 fixture，也不能用 runner 的成功 marker 替代
owner/state/happens-before 论证；这一区分保留 ADR 0011 要求的 rubric、报告结构
和非代码答案边界。

fixed 路径沿用生产 `sleep()`。broken 路径额外嵌套一次 `push_off()`，只为把错误
窗口固定在 origin hart；取得 `p->lock` 后配对 `pop_off()`，使 `sched()` 仍看到
`noff==1`。专用 wrapper 把顺序改为：

```text
WAIT_CHECK -> release(lk) -> WAIT_RELEASED
PRODUCER_READY -> WAKE_MISS while target RUNNING/chan absent
WAKE_DONE -> acquire(p.lock) -> WAIT_PUBLISH SLEEPING/expected chan
```

parent 只有主动读取 `ready=1 && SLEEPING && expected chan && wake_miss=1 &&
killed=0` 后，才调用专用 rescue。kill 会把 `SLEEPING` 改为 `RUNNABLE`，会破坏
反例，因此不能作 oracle；timeout 也只终止失控运行。

允许副作用仅为临时源码/build、私有 `fs.img` 和短寿命 QEMU/driver 进程组。
每轮必须回收两个 child，确认 condition lock 未锁、channel 无 target、trace
清零、gate 全零且 active 关闭；runner 最后 `make clean`、逆向 patch、比较快照，
并确认共享工作树内容/索引指纹与共享 `fs.img` digest 不变。

## Oracle、证据、失败路径和局限

| 维度 | 触发器 | 必须观察到 | 允许副作用与资源结果 |
| --- | --- | --- | --- |
| S | pinned baseline + 11-path patch | context 保存集合、锁交接、内存序、`noff/intena`、sleep/wakeup 与 targeted hook 顺序全部成立 | 只写临时导出；逆向后逐文件相同 |
| F | fixed + `preempt/pipe1/killstatus` | `lk+p.lock -> publish -> WAKE_MATCH -> RUNNABLE -> recheck`，无 miss/rescue；定向回归通过 | child 回收，锁/channel/trace/gates 归零 |
| B | broken 专用 wrapper | 关中断固定 hart 的窗口中，wake 在 target 仍 `RUNNING/chan absent` 时 miss；其后主动读到 `ready=1/SLEEPING/expected chan/killed=0` | 记录 oracle 后一次 rescue；不靠 kill/timeout 判成功 |
| C | `CPUS=2` 双 gate与 scheduler gate | producer/waiter 位于不同 hart；origin skip 和 release gate 后由另一 hart select/resume；四轮 generation 关系一致 | 32 槽未溢出；每轮 process/lock/channel/gate 清理闭合 |
| R | N/A | 无 crash、磁盘顺序或重启主张 | 私有镜像仅用于隔离，不产生恢复结论 |

这组有限交错能反驳“只要 channel 相同就不会丢 wake”“wakeup 直接交付资源”与
“进程必须回到原 hart”等错误模型；它不能穷尽调度交错、证明公平性、替代
RISC-V/DMA memory model 或构成形式化验证。instrumentation、关中断自旋和
强制 migration 会改变时序；报告只使用命名事件间的 happens-before，不使用
串口延迟。

## 退出产物与后续单元

提交一份报告包，至少包含：

1. scheduler/进程 continuation 与两次 `p->lock` 所有权交接图；
2. fixed/broken 的事件表，逐行解释 `lk owner`、`p->lock owner`、state、chan、
   ready、`noff` 与 hart；
3. bad-state 主动快照、rescue 发生时点和四轮 generation 重复性；
4. focused/related/quick/full 结果，以及进程、锁、channel、trace、gate、临时
   patch 和进程组清理证明；
5. S/F/B/C/R 边界与不能推出的结论。

配套[调度与同步问题](../questions/scheduling-and-synchronization.md)用于从全景到
源码细节复核该报告。旧路径的 `PROC-01/02/09/10`、`BOOT-10` 与
`SYNC-01/03/04/09` 已在 verified 晋级时替换为兼容入口。下一步的虚拟内存单元展开 page table、
fault、权限和 TLB；后续 Copy-on-Write 项目再把本单元的并发交接与页引用所有权
合并。
