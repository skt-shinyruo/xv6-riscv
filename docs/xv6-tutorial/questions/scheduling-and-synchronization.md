# 调度与同步问题

本页由 `core.scheduling-and-synchronization` 拥有。先完成[调度、同步与等待](../core/scheduling-and-synchronization.md)
及同一份证据报告，再查看[答案与证据标准](answers/scheduling-and-synchronization.md)。

稳定源码入口为 `kernel/proc.c:scheduler()`、`kernel/proc.c:sched()`、
`kernel/proc.c:sleep()`、`kernel/proc.c:wakeup()`、`kernel/proc.c:cpuid()`、
`kernel/proc.c:mycpu()`、`kernel/swtch.S:swtch`、`kernel/spinlock.c:holding()`、
`kernel/riscv.h:r_tp()`、`kernel/kernelvec.S:kernelvec`、`kernel/pipe.c:piperead()`、
`kernel/pipe.c:pipewrite()`、`kernel/bio.c:bread()`、`kernel/log.c:begin_op()`、
`kernel/sleeplock.c:acquiresleep()` 和 `kernel/sleeplock.c:releasesleep()`；答案应
沿这些 path:symbol 复算，而不是依赖行号。

## 问题

### SCHED-00

从用户态 timer 到进程在另一 hart 继续执行，要经过哪些 trap、state、锁与
continuation 边界？条件等待发生在这条路径中时，控制边和 happens-before 边
怎样叠加？

证据要求：画出 `timer -> yield -> sched -> swtch(process,scheduler) ->
scheduler -> swtch(scheduler,process) -> resume`，并并列 waiter/producer 的两把
锁；标出哪些事实来自 #8/#9，哪些由本单元新增。

### SCHED-01（原 PROC-01）

`scheduler()` 为什么持有 `p->lock` 跨越 `swtch()`，并让切换后的 continuation
负责释放？反方向从进程切回 scheduler 时，谁接住同一把锁？

证据要求：分别追踪首次 `forkret()`、从 `sched()` 恢复和 scheduler 从
`swtch()` 恢复三个入口；不能把跨 continuation 的交接写成普通 caller 释放。

### SCHED-02（原 PROC-02）

`sched()` 为什么同时要求持有 `p->lock`、`noff==1`、中断关闭且 state 不是
`RUNNING`？每一项若缺失，会暴露哪类不一致或死锁？

证据要求：连接 `yield/sleep/kexit` 的调用前状态与 `sched()` 四个 panic guard，
并分别解释另一把未释放 spinlock 与一次额外 `push_off()` 怎样令 `noff!=1`。

### SCHED-03

`swtch()` 为什么只保存 `ra/sp/s0-s11`，而不保存 caller-saved 寄存器、用户
trapframe、`satp`、特权级或 `tp`？恢复的究竟是进程还是一条 kernel continuation？

证据要求：逐项对照 `struct context`、RISC-V C ABI、kernel stack 和
`swtch.S` 的 store/load 集合；不要把它与 trampoline 或 `sret` 混合。

### SCHED-04（原 SYNC-01）

spinlock 的 acquire/release memory order 与 `push_off/pop_off` 各自防止什么？
为什么关闭本 hart 中断既不阻止另一 hart，也不替代跨 hart 可见性？

证据要求：追踪 `__ATOMIC_ACQUIRE/__ATOMIC_RELEASE`、`noff/intena` 和
`holding()`；给出嵌套两把锁后两次 `pop_off()` 的状态变化。

### SCHED-05（原 BOOT-10）

进程从 origin hart 被抢占并在另一 hart 恢复后，为什么不能从进程 context
恢复旧 `tp`？用户 trap 与 `kernelvec` 的 kernel-mode trap 各自怎样保留 hart-local
`tp`？`intena` 又为何需要随暂停的 kernel continuation 保存？

证据要求：比较 `tp`、`struct cpu`、`mycpu()`、`kernelvec` 的注释/保存集合、
`sched()` 的局部 `intena` 和 `prepare_return():kernel_hartid`；用报告中的
origin/resume hart 关系复核。

### SCHED-06（原 PROC-09）

请用 `lk` 与 waiter `p->lock` 证明生产版 `sleep(chan, lk)` 不会丢失唤醒。
为什么 waiter/producer 仅使用同一 `chan` 仍然不够？这条证明与醒后使用
`while` 复查谓词分别保护什么？

证据要求：列出 producer 先取得 `lk` 与 waiter 先取得 `p->lock` 两种全序，
指出 producer 必须持 `lk` 写谓词；另给一个竞争者先消费的轨迹，说明 `while`
保护醒后谓词，却不是关闭 check-to-sleep 窗口的那把锁。

### SCHED-07（原 SYNC-09）

broken 轨迹中，哪一个最短偏序构成了可归因的 lost wakeup？为什么“程序超时”
或“最终被 kill 唤醒”都不是该结论的 oracle？

证据要求：从 `WAIT_RELEASED -> PRODUCER_READY -> WAKE_MISS -> WAKE_DONE ->
WAIT_PUBLISH -> BAD_ORACLE` 逐项读取 state/chan/ready/owner；bad-state 记录前不得
使用 rescue 或 kill。

### SCHED-08（原 PROC-10）

`wakeup()` 为什么只把所有匹配 waiter 设为 `RUNNABLE`，而不直接把资源交给某个
进程？`while` 重查怎样处理竞争者、broadcast 与已经观察到的取消唤醒？为什么
不持条件锁的 `kkill()` 不继承生产者协议的无丢失保证？

证据要求：把 `WAKE_MATCH -> scheduler select -> reacquire lk -> WAIT_RECHECK`
与 pipe 或 sleeplock 的谓词循环连接起来。

### SCHED-09（原 SYNC-03）

哪些锁可以跨等待，哪些不能？为什么 `sleep(chan, lk)` 的条件锁交接是例外，
而“持有两把任意 spinlock 后 sleep”会触发 `sched locks`？为什么中断上下文不能
调用会经过 `bread()`、`acquiresleep()` 或 `begin_op()` 的睡眠路径？

证据要求：画出 `lk -> p->lock`、wakeup 的 `lk -> target p->lock` 和恢复时
`release p->lock -> reacquire lk`；同时检查 pipe、buffer cache、日志和 sleeplock
的实例，说明中断上下文的当前进程/可睡眠前提。

### SCHED-10

给定完整报告，哪些事件关系证明 fixed、broken、migration 与 cleanup，哪些只是
instrumentation 造成的时序？若把 `CPUS=2` 改为 1，哪些 gate 会失去推进者？

证据要求：核对四个连续 generation、32 槽上界、进程/锁/channel/trace/gate
归零、2-hart quick 与 1-hart full；最后明确公平性、全交错、DMA 与 R 均未证明。

## 提交边界

整组问题只引用一份调度、同步与 lost-wakeup 报告包，不建立同步副本。旧路径的
`PROC-01/02/09/10`、`BOOT-10` 与 `SYNC-01/03/04/09` 已在本单元完成非作者
走查并晋级 verified 时替换为兼容入口。
