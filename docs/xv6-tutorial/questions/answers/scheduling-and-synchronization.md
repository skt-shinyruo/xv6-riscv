# 调度与同步问题：答案与证据标准

答案必须引用本次隔离运行的关系字段；固定 pid、固定绝对地址、串口延迟或单个
`PASS` 都不能替代事件与资源 oracle。

## SCHED-00

timer 由 user/kernel trap 路由到 `yield()`；进程持 `p->lock` 发布 RUNNABLE，先经
`swtch(process,scheduler)` 把 continuation 交给当前 hart 的 scheduler；scheduler
接住锁、清 `c->proc` 后释放。下一次选择时 scheduler 再经
`swtch(scheduler,process)` 返回进程 continuation，进程接住 `p->lock` 后继续，
也可能被另一 hart 重新取得锁、发布 RUNNING 并切回。若进程在条件上等待，另有
`lk -> p->lock -> SLEEPING` 与 producer `lk -> ready -> p->lock -> RUNNABLE` 的
happens-before。#8 只追到 timer/yield，#9 只给状态/生命周期；本单元闭合 context、
锁与跨 hart 交接。

## SCHED-01

scheduler 先在 `p->lock` 下检查 RUNNABLE、写 RUNNING 与 `c->proc`；若切换前释放，
另一 scheduler 或 wake/kill 会观察到无法对应 CPU ownership 的中间状态。新进程
在 `forkret()` 接住并释放锁；暂停进程从 `sched()` 的 `swtch` 返回后接住，随后
由 `yield/sleep` caller 释放。反向切换时进程持锁发布非 RUNNING 状态，scheduler
从自己的 `swtch` 返回后接住并最终释放。通过答案必须讲清两个方向。

## SCHED-02

`p->lock` 保护 state/chan/CPU ownership；若进入 `sched()` 时 state 仍是 `RUNNING`，
该 continuation 会被交给 scheduler 但没有可供扫描的 `RUNNABLE` 发布，因而可能
永远无人选择它；这不是两个 CPU 同时执行同一进程。中断开启会在切换准备期重入
调度或锁路径；`noff==1` 说明当前唯一关闭中断层就是 `p->lock`。多持一把锁后
切走会让其他进程永久等一个由暂停 continuation 才能释放的锁；即使没有第二把
锁，一次未配对的额外 `push_off()` 也会令 `noff==2` 并触发 `sched locks`。panic
guards 是调用契约检查，不是调度策略。

## SCHED-03

`swtch` 恢复的是 C 调用链 continuation：callee-saved `s0-s11`、返回点 `ra` 和
kernel stack `sp` 足以继续 C 函数。跨调用仍活跃的值可由编译器放入这些
callee-saved 寄存器、重新计算或 spill 到当前 stack；caller-saved 寄存器本身
不是保存契约。用户寄存器在 trapframe，页表/特权转换由 trampoline/sret 处理。
`tp` 属于当前 hart，保存旧值反而会让 `mycpu()` 指向错误 CPU。

## SCHED-04

atomic acquire 阻止临界区 load/store 越过取锁，release 在解锁前发布临界区写入，
两者经同一锁建立跨 hart 可见性。`push_off` 记录第一次关闭前的 `intena` 并递增
`noff`；嵌套第二次不覆盖初值，第一次 `pop_off` 只降到 1，第二次降到 0 才按
原状态开中断。进程切回 origin scheduler 前取得 `p->lock`，使 origin CPU 的
`noff` 从 0 到 1；scheduler 接住后 release 回到 0。destination scheduler 后来
acquire 时，其 CPU 的 `noff` 独立从 0 到 1，进程在该 hart 接住并 release 后再
回到 0；迁移不把 `noff` 从一个 CPU 搬到另一个 CPU。它只约束当前 hart；另一
hart 仍靠 atomic lock 互斥。

## SCHED-05

`tp` 是当前 hart id；进程 migration 后恢复旧值会让 `cpuid/mycpu` 访问 origin
CPU 的 `struct cpu`。因此 `swtch` 不保存 tp，返回用户前以当前 `r_tp()` 更新
trapframe `kernel_hartid`。kernel-mode trap 由 `kernelvec` 在当前 kernel stack
保存 caller-saved 集合，但明确不保存/恢复 `tp`；注释说明这正是为了允许 trap
处理期间迁移 CPU。`intena` 则描述这条 kernel continuation 在进入嵌套关中断前
的逻辑状态；它是 `sched` 的 C 局部值，可在 callee-saved context 或进程 kernel
stack 中跨切换保存，恢复后写入实际接住 continuation 的 CPU。报告必须看到
`origin != resume`，但不能推广调度公平性。

## SCHED-06

若 producer 先取得 `lk`，它写 ready 后 waiter 才能重取 `lk`，于是 while 看见真
而不睡。若 waiter 先取得 `lk`，它在释放 `lk` 前已取得 `p->lock`；producer 即使
随后写 ready，也会在 wakeup 获取同一 `p->lock` 时等待，直到 waiter 发布
SLEEPING 并交给 scheduler。channel 只做匹配键，不保护谓词；缺少同一 `lk` 会
破坏 check-to-sleep 证明。`while` 则在醒来并重取 `lk` 后防止竞争者已消费、
broadcast 或取消造成假谓词继续；它不承担前述窗口的原子交接。

## SCHED-07

broken wrapper 先释放 lk 且尚未取得 p lock；producer 写 ready 时 target 仍
RUNNING、chan 为空，所以 wake scan 明确 miss。wake 已完成后 target 才发布
SLEEPING/expected chan，主动 snapshot 同时看到 ready=1、miss=1、killed=0，才
构成稳定坏状态。timeout 只能说明没结束；kill 会主动改成 RUNNABLE，反而抹掉
关键状态。rescue 只能在 BAD_ORACLE 之后用于回收。

## SCHED-08

wakeup 不拥有 pipe 字节或 sleeplock；它只在 target `p->lock` 下发布调度资格，
而且会唤醒同 channel 的所有 waiter。恢复者先释放 p lock、重新取得条件锁，再
在 while 中判断资源是否仍可用；一个竞争者消费后，其他恢复者会再次 sleep。
已经观察到的 close/kill 等取消 wake 也不会让 waiter 在假谓词下直接消费；但
`kkill()` 不持条件锁，只唤醒当时已经 SLEEPING 的进程，所以它可能落在检查与
发布之间而丢失。调用方必须另有 killed/safe-point 协议，不能把 `while` 当成
取消 wake 无丢失的证明。

## SCHED-09

waiter 和 producer 都按 `lk -> target p->lock` 方向，不形成反向环；waiter 从
sched 恢复后先释放 p lock，才 reacquire lk。sleep 只释放参数 lk，无法替调用者
释放其他 spinlock，所以额外锁会让 noff 不为 1。`bread()` 会取得 buffer sleeplock
并可能等待 VirtIO，`begin_op()` 会取得日志锁并可能 sleep；外部中断上下文没有可
信赖的 sleepable current process，也可能打断正持有这些锁的进程，因此不能从中断
路径调用它们或 `acquiresleep()`。timer interrupt 在 `myproc()!=0` 时能 `yield()`，
并不等于任意中断上下文都满足这些可睡眠调用契约。

## SCHED-10

fixed 必须是零 miss/零 rescue/一次 match；broken 必须先有一次 miss，再有主动
bad snapshot 和一次 rescue。两者都要求 origin skip、other-hart select/resume、
child 回 baseline、condition unlocked、target absent，以及 clear 后 channel/trace/
gate/active 全零。四个连续 generation 反驳残留 gate；CPUS=1 full 证明未 arm
路径不依赖第二 hart。关中断 spin、强制 migration 和 instrumentation 会改变时序，
所以报告不证明公平性、全交错、DMA memory order、持久化恢复或形式化正确性。
