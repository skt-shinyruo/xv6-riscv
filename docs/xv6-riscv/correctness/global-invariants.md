# 全局正确性不变量

本文把分散在进程、页表、文件、inode、buffer、日志和设备文档中的局部结论组合成一组跨子系统证明义务。它不是 API 摘要，也不假定“测试通过”就等价于证明成立。目标是回答三个问题：一个对象在任意时刻由谁拥有，哪些状态必须一起发布，以及一个局部改动会依赖哪些更高层前提。

局部算法仍以专题文档为准：调用限制见[调用上下文契约](../kernel/call-context-contracts.md)，锁实现见[同步与锁](../kernel/synchronization.md)，日志块数证明见[资源上界](../kernel/resource-bounds.md)，不可信输入边界见[信任与失败模型](../architecture/trust-and-failure-model.md)。

## 1. 证明边界

本文只为当前仓库的模型建立结论：固定进程表、单线程进程、每个进程至多在一个 hart 上执行、共享内核页表、单根文件系统、同步 VirtIO block I/O、无动态卸载设备。加入用户线程、共享地址空间、异步 I/O、多个文件系统或运行期修改内核映射后，下列多项证明会失效。

本文区分四类陈述：

| 类别 | 含义 | 例子 |
|---|---|---|
| 结构不变量 | 每个可观察稳定状态都必须满足 | `RUNNING` 进程恰由一个 `cpu.proc` 指向 |
| 转换前置条件 | 进入函数或状态转换前必须成立 | `sched()` 只允许持有当前 `p->lock` 这一把 spinlock |
| 发布不变量 | 对象只有在完整构造后才可被其他执行者看到 | child 最后才置为 `RUNNABLE` |
| 环境前提 | 当前实现未自行强制、但正确性依赖 | QEMU 硬件更新 PTE A/D 位 |

“稳定状态”通常指锁外可被下一次系统调用、scheduler 或中断观察的边界。函数内部允许短暂打破计数对应关系，例如 `sys_dup()` 先安装 fd、再增加 file 引用；正确性依赖同一进程没有另一个用户线程在中间观察。

## 2. 依赖总图

```text
平台交接、RVWMO、页表和设备语义
        |
        +--> spinlock acquire/release 与 IRQ 屏蔽
        |       |
        |       +--> sleep/wakeup 条件协议
        |       |       |
        |       |       +--> scheduler、wait、pipe、console、日志、VirtIO
        |       |
        |       +--> 对象身份与引用计数串行化
        |
        +--> Sv39、satp、sfence.vma
        |       |
        |       +--> 页表私有修改、trap 往返、lazy fault、exec 提交
        |
        +--> 同步 block completion 与缓存身份唯一性
                |
                +--> redo log 顺序、inode/目录持久状态、orphan 回收
```

上层结论不能反向替代下层前提。例如日志原子性不能弥补 buffer identity 被复用，`sfence.vma` 也不能替代 VirtIO DMA 所需的内存发布顺序。

## 3. hart、CPU 与进程

### 3.1 身份与运行排他

1. 活跃 hart id 必须可索引 `cpus[NCPU]` 和早期 `stack0`；代码不检查 `CPUS > NCPU`。
2. 内核态 `tp` 保存当前 hart id。用户可修改自己的 `tp`，所以 `uservec` 必须在进入 C 前从 trapframe 恢复内核 hart id。
3. 该 hart 正执行 `p` 的内核或用户流时 `c->proc==p`。进程 `swtch()` 回 scheduler 后到 `scheduler()` 执行 `c->proc=0` 前还有一个短 handoff 窗口：控制流已在 scheduler 栈，但字段仍为 `p`；此时持 `p->lock` 且中断关闭。窗口结束后的 scheduler 扫描/idle 稳定状态使用 `c->proc==0`。
4. 一个 `RUNNING` 进程只允许出现在一个 hart 上。scheduler 在持 `p->lock` 时把 `RUNNABLE` 改成 `RUNNING`，另一 hart 因同一把锁不能同时认领。
5. 进程不在两个 hart 同时执行，是页表、`ofile[]`、`cwd` 和 trapframe 无独立运行期锁仍然成立的基础。

### 3.2 进程状态机

```text
UNUSED --allocproc--> USED --完整发布--> RUNNABLE
                                      |
                                      v
                                  RUNNING
                                  /  |   \
                              yield sleep  exit
                                |     |      |
                                v     v      v
                           RUNNABLE SLEEPING ZOMBIE --wait--> UNUSED
```

| 状态 | 必须成立 |
|---|---|
| `UNUSED` | 槽可重新认领；没有已发布 parent、用户页表或资源引用 |
| `USED` | 构造中且不可调度；访问受保护字段时持槽锁，构造者可在设置 parent 等已定义阶段暂时释放它 |
| `RUNNABLE` | trapframe、pagetable、context、parent、fd/cwd 引用均已完整 |
| `RUNNING` | 某个 scheduler 已持锁完成认领；该进程内核栈只被该执行流使用 |
| `SLEEPING` | `chan` 与状态在 `p->lock` 下共同发布，等待条件由对应条件锁保护 |
| `ZOMBIE` | 已关闭运行资源，但 pid、xstate、trapframe、页表和槽保留给 parent `wait()` |

`RUNNABLE` 是构造提交点，`ZOMBIE` 是退出发布点。把 child 提前设为 `RUNNABLE` 会暴露半初始化 parent/fd/cwd；在旧内核栈仍使用时提前释放 zombie 会造成栈和页表 use-after-free。

### 3.3 parent 指针生命周期

`parent` 是裸 `struct proc *`，但当前协议避免 ABA：

1. 已发布进程的 parent 只在 `wait_lock` 下读写。
2. 进程退出时持 `wait_lock`，先把所有 child 重新托管给永久存活的 `initproc`，再发布自身 `ZOMBIE`。
3. 上级 parent 只有在同一把 `wait_lock` 下看到该进程为 `ZOMBIE` 后才回收并复用槽。
4. 因而一个槽成为 `UNUSED` 前，不再存在已发布 child 指向它。
5. 构造失败的 `USED` 槽可以直接 `freeproc()`，因为 parent 尚未发布。

这项证明依赖单线程进程：同一个 parent 不会一边在 `kfork()` 中附加 child，一边由另一个用户线程执行 `kexit()`。

## 4. 锁、等待与内存发布

### 4.1 spinlock 层

`acquire()` 的 acquire 原子操作和 `release()` 的 release 原子操作形成临界区的跨 hart happens-before。`push_off()/pop_off()` 额外防止当前 hart 在持锁时被会重取同锁的中断打断；它不阻止其他 hart 并行，也不替代设备 DMA barrier。

持任意 spinlock 时本 hart supervisor interrupt 关闭。`sched()` 要求 `noff == 1`，从而强制进入时只剩当前 `p->lock`。这个计数只跟踪 spinlock/显式 `push_off()`，不能发现仍持有的 sleeplock。

### 4.2 条件等待的完整协议

一个不丢失唤醒的条件必须同时满足：

```text
waiter:   lock L -> while (!predicate) sleep(chan, L) -> consume predicate
producer: lock L -> change predicate -> wakeup(chan) -> unlock L
```

`sleep()` 先取得 waiter 的 `p->lock`，再释放 `L` 并发布 `chan/SLEEPING`。producer 在取得同一 `L` 前不可能越过 waiter 的条件检查；在 waiter 释放 `L` 后，又必须等待 `p->lock` 才能扫描其睡眠状态。因此“条件已成立但 waiter 尚未登记”没有无锁窗口。

只对同一个 `chan` 调用 `wakeup()` 不足以证明正确。若 producer 不持保护谓词的同一把锁，仍可在 waiter 检查后、进入 `sleep()` 前完成更新和扫描，造成永久睡眠。

`wakeup()` 只把匹配者设成 `RUNNABLE`，不授予资源、没有公平性，也不发送 reschedule IPI。目标 hart 若正在 `wfi`，通常要等下一次本地 timer 或设备中断才会重新扫描。

### 4.3 锁与资源等待图

下图同时记录 CPU 锁嵌套和持有 sleeplock 时可能等待的资源；只看 spinlock 图不足以发现 I/O 环：

```text
wait_lock -------> p->lock -------> pid_lock / kmem.lock / ftable.lock / itable.lock
condition lock --> waiter p->lock

itable.lock ------> inode sleeplock internal lock
inode sleeplock --> buffer sleeplock --> vdisk_lock --> waiter p->lock
buffer sleeplock -> log.lock ------> bcache.lock
pipe/console lock -----------------> p->lock / kmem.lock
log.lock --------------------------> p->lock / bcache.lock
```

关键禁止项：

- 持 inode/buffer sleeplock 调用可能因日志容量睡眠的 `begin_op()`；已有事务可能正等待该对象锁。
- 持已登记 home buffer 的 sleeplock 调用最后一个 `end_op()`；同步 commit 会再次 `bread(home)` 并等待同一把锁。
- 持任意额外 spinlock 进入 `sched()`。
- 从设备 IRQ 调用 `bread()`、`acquiresleep()`、`begin_op()` 或其他可睡眠路径。

## 5. 物理页与页表

### 5.1 物理页唯一所有者

每个 `kalloc()` 返回页在重新 `kfree()` 前必须只有一个释放责任。allocator 不保存引用计数，也不检测双重释放。典型 owner 包括用户 PTE、页表层级、trapframe、kernel stack、pipe 页面和 VirtIO queue 页面。

发布给用户或硬件前，新页必须清零或被完整可信数据覆盖：

- 用户匿名/lazy 页清零，避免泄露旧内核内容。
- 页表页清零，保证未建立 PTE 无效。
- VirtIO ring 页面清零，建立初始 producer/consumer 状态。

allocator poison 只用于暴露错误，不构成安全清零或 double-free 检测。

### 5.2 PTE 与地址空间

1. 用户可访问叶 PTE 必须有 `PTE_V|PTE_U` 和与用途一致的 R/W/X 权限。
2. trampoline 与 trapframe 在用户页表中存在但没有 `PTE_U`。
3. 页表函数无内部锁；运行中修改依赖当前进程独占自己的地址空间。
4. child 页表在 `RUNNABLE` 前私有，exec 新页表在提交前私有。
5. lazy hole 属于逻辑 `[0,p->sz)`，但没有物理页和有效 PTE；fork、缩容、销毁必须容忍它。
6. 成功 lazy fault 不推进 `epc`，返回用户态后重试原指令。
7. 当前所有地址空间 ASID 都为 0，切换前后做本 hart 全量 `sfence.vma`。

当前无需跨 hart TLB shootdown，因为一个地址空间不并行执行且只由 owner 修改。引入共享地址空间或运行期内核映射会立刻打破该证明。

### 5.3 exec 提交

`kexec()` 在临时页表完成 ELF、栈和 argv 后，才交换 `p->pagetable/p->sz` 并更新入口寄存器。提交前失败释放新表、保留旧映像；提交后旧表由当前内核执行流同步释放。此时硬件使用内核页表和进程内核栈，所以释放旧用户页表不会破坏当前 C 栈。

exec 只定义新 PC、SP、`a0/a1` 及映像相关状态；其余 GPR 可能保留旧值，首进程还可能带 allocator poison。用户入口 ABI 不得依赖未指定寄存器。

## 6. fd、file、inode 与 pipe

### 6.1 三层引用

```text
p->ofile[fd] --一个稳定边界引用--> struct file.ref
FD_INODE/DEVICE file -------------> struct inode.ref
目录项 ----------------------------> dinode.nlink
```

三者不能互相替代：close fd 只减少 file ref；最后一个 file ref 才释放 inode ref；unlink 只减少 `nlink`，已打开引用可继续使用 inode。

在系统调用稳定入口/出口，每个非空 `ofile[]` 槽对应一个 file ref。内部瞬间例外包括：

- `sys_dup()` 先用 `fdalloc()` 安装指针，再 `filedup()`。
- `sys_open()` 先安装 fd，再填写新 file 的 type/ip/权限。
- `kexit()` 先 `fileclose()`，再清空槽。

这些例外只有在同一进程没有并发线程时安全。增加线程后需要保护 `ofile[]`，并为 `argfd()` 取得的指针增加临时 pin。

### 6.2 pipe 生命周期

pipe 页只有在 `readopen==0 && writeopen==0` 时释放。完整无 UAF 证明还依赖：

1. 正在执行 read/write 的当前进程不能并发 close 自己的 endpoint。
2. 其他进程若正在使用或睡眠，必然仍通过自己的 fd 持有对应 file ref。
3. 因而有 waiter 时相应 open flag 不会归零，等待通道所在 pipe 页保持有效。

pipe writer 在有空间时一直持锁。成功写入超过 `PIPESIZE` 的请求必然至少一次因满缓冲进入 `sleep()`；reader 不可能在 writer 不释放锁时“同步消费”。

## 7. buffer、日志与文件系统

### 7.1 buffer identity

对每个 `(dev, blockno)`，buffer cache 至多有一个缓存对象。`bcache.lock` 保护 identity、引用和 LRU；`b->lock` 保护数据。取得 `bread()` 返回的 buffer 后必须持其 sleeplock，`brelse()` 后不得再访问 `data`，因为槽可立即被复用为另一个块。

日志首次登记 home buffer 时 pin 它，直到 install 完成。pin 保持 identity，不等同于持有 sleeplock；事务参与者必须在完成修改后释放 buffer 锁，commit 才能重新读取它。

### 7.2 redo 事务

日志组的提交点是非零 header 写入完成，而不是 `end_op()` 返回。正确性依赖以下顺序：

```text
修改并登记 home buffers
  -> write all log data
  -> write non-zero header       # commit point
  -> install all home blocks
  -> clear header
```

所有 outstanding operation 归零后才 commit。一个已经 `end_op()` 的调用可在其他 operation 仍 outstanding 时返回，此时尚未获得持久性保证。持续重叠的 operation 还可能让 commit 无限期推迟；实现没有 epoch、定时提交或 `fsync`。

每个 operation 实际登记的 unique home blocks 必须不超过 `MAXOPBLOCKS`。admission 不按线程记账，`log_write()` 也只看到全局 outstanding，因此这是调用图约定，不是运行期强制配额。

### 7.3 inode 删除与恢复

运行期 inode 只有在 `ref==1 && nlink==0` 的最后 `iput()` 才截断并清 type。若系统在 unlink 已提交但最后内存引用消失前崩溃，重启后内存 ref 丢失；`ireclaim()` 必须在日志恢复之后扫描 `type!=0 && nlink==0` 的 dinode 并完成回收。

该结论只适用于良构磁盘。错误的 `nlink`、block address、bitmap 或 log header 会被实现高度信任，可能造成误回收或元数据覆盖；详见[文件系统一致性](../filesystem/filesystem-consistency.md)。

## 8. trap、中断与设备

1. `stvec` 是每 hart 状态；内核执行窗口指向 `kernelvec`，返回走廊关闭中断后改为 `uservec`。
2. 用户 trap 保存全部用户 GPR；kernel trap 依赖 C ABI，只显式保存 caller-saved GPR 加 `gp`。
3. IRQ handler 不得睡眠；它只确认设备状态、发布条件并唤醒进程。
4. 一次 supervisor external trap 只执行一次 PLIC claim。UART/VirtIO handler 可以在设备内部批量消费事件，但这不是多次 claim。
5. 设备 ACK 必须先于 PLIC complete。未知 level IRQ 若未清除设备条件，即使 complete 也会再次 pending，可能形成中断风暴。
6. VirtIO descriptor/ring/status 的 CPU 访问由 `vdisk_lock` 和发布 fence 协调；设备 DMA 不取得 CPU 锁。
7. VirtIO completion 只发布请求完成，不等同于真实介质掉电持久化；日志没有 FLUSH/FUA 保证。

## 9. 关键 happens-before 链

| 发布者 | 发布动作 | 观察者 | 保证的状态 |
|---|---|---|---|
| boot hart | 初始化共享对象，fence，`started=1` | 其他 hart 轮询后 fence | kernel page table、锁、设备全局状态已初始化 |
| fork parent | child 锁下 `state=RUNNABLE` | scheduler 取得 child 锁 | parent、trapframe、页表、fd/cwd 已完整 |
| sleeper | `p->lock` 下 `chan/state=SLEEPING` | producer 的 `wakeup()` | 不丢失同一条件锁保护的谓词变化 |
| device | used entry/status 后提高 used idx | IRQ handler fence 后读取 | 完成项和 status 对 CPU 可见 |
| log writer | log data 完成后写 header | reboot recovery | 非零 header 指向可重做的完整 log data |
| exiting child | `wait_lock -> p->lock` 下发布 ZOMBIE | parent 同锁序扫描 | xstate 和保留资源可安全回收 |

`volatile` 不是跨 hart或 DMA 的完整同步原语。`sfence.vma` 只同步地址翻译；CPU 原子 fence 不等于磁盘 flush；三者不能互换。

## 10. 修改时的证明清单

1. 列出新增或改变的对象 owner，以及成功、失败、阻塞和取消路径上的移交点。
2. 标明状态何时首次被其他 scheduler、hart、IRQ 或设备观察；发布前是否已完整初始化。
3. 对每个 wait 写出谓词、条件锁、通道和 producer，确认 producer 在同一锁下先改条件再唤醒。
4. 同时画 spinlock 图、sleeplock/资源等待图和 DMA 所有权图，不能只统计 `acquire()`。
5. 对页表修改说明并发排他、TLB/指令同步和失败回滚。
6. 对 fd/inode/buffer 说明身份引用和内容锁分别由谁维持。
7. 对文件系统修改计算 unique home-block 集合、pin 峰值、临时 buffer 和 crash 窗口。
8. 区分用户错误、资源耗尽、磁盘损坏和设备异常最终是返回、kill、panic、等待还是部分副作用。
9. 为证明前提选择能控制交错或故障点的测试；随机压力只能提高置信度。

## 11. 验证入口

- [故障注入](../verification/fault-injection.md)给出资源耗尽、调度交错和精确 crash point 的实验协议。
- [源码到测试追踪矩阵](../reference/source-test-traceability.md)列出每项不变量目前由哪些测试覆盖、哪些仍是静态证明。
- [资源失败矩阵](../reference/resource-failure-matrix.md)统一记录固定容量和退出方式。
- [端到端流程](../flows/)用于核对同一对象在多层调用之间的状态变化。

全局证明的用途不是声称 xv6 没有缺陷，而是准确标出结论成立的范围。任何依赖“单线程进程”“可信镜像”“同步 QEMU VirtIO”或“固定初始化后只读”的地方，都应在扩展设计开始前被当作待解除的显式约束。
