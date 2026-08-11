# 内核调用上下文契约

本文把分散在 `kernel/spinlock.c`、`kernel/sleeplock.c`、`kernel/proc.c`、`kernel/vm.c`、`kernel/bio.c`、`kernel/log.c`、`kernel/fs.c`、`kernel/file.c`、`kernel/pipe.c`、`kernel/uart.c` 和 `kernel/virtio_disk.c` 中的调用前提集中成一份可审计矩阵。目标不是罗列所有函数，而是回答六个跨模块问题：调用前持有什么锁、函数内部会取什么锁、是否可能睡眠、是否分配资源、是否要求日志操作区间、能否从外部设备中断上下文调用。

各子系统的算法仍以专题文档为准：同步机制见[同步与锁](synchronization.md)，进程锁交接见[进程与调度](processes-and-scheduling.md)，存储协议见[存储栈](storage-stack.md)和[资源上界](resource-bounds.md)。本篇描述当前调用图的前置条件；新增调用者必须重新检查，而不能只因为函数在 C 类型上可见就认为任意上下文都能调用。

## 1. 术语和判定规则

本文使用四种执行环境：

| 环境 | 当前进程 | 可以调度/睡眠 | 典型入口 |
|---|---|---|---|
| 普通进程上下文 | 有 `myproc()`，使用该进程内核栈 | 可以，但睡眠前不能持普通 spinlock | 系统调用、`forkret()`、被时钟抢占后恢复的内核代码 |
| 用户 trap 的进程上下文 | 有当前进程，刚从 U-mode 进入 | 可以在完成入口状态保存后分配、退出或调度 | `usertrap()` 的 syscall、page fault、timer 分支 |
| 内核 trap 的进程分支 | 可能有当前进程，也可能是 scheduler context | 只有确认 `myproc()!=0` 且锁不变量满足时才能 `yield()` | `kerneltrap()` |
| 外部设备中断上下文 | 不拥有可供 handler 任意阻塞的调用语义 | 不能执行会调用 `sleep()/sched()` 的路径 | `uartintr()`、`virtio_disk_intr()` |

“不睡眠”只表示正常返回路径不会主动进入 scheduler，不表示有常数时间上界。spinlock 竞争、UART MMIO 轮询和 `wakeup()` 的全表扫描都可能很慢。若路径触发 `panic()`，它会进入终止性忙循环，不能把该路径当作可返回的“不睡眠”操作。“可从 IRQ 调用”还要求调用者满足对象生命周期和锁顺序；它不是对任意参数的许可。

`kalloc()` 只使用 spinlock，因此不会睡眠，但可能失败。`copyin()/copyout()` 可能通过 `vmfault()` 间接调用 `kalloc()`。表格把这种行为记作“可能分配”，即使调用者没有显式分配语句。

日志列中的“区间”指匹配的 `begin_op()/end_op()`，不是系统调用。`log_write()` 只检查全局 `log.outstanding>=1`，不追踪当前线程是否拥有预留；所以“已有另一个线程的区间”绝不能替代当前调用链自己的区间。

实现中的 defensive check 并不统一。spinlock 递归/错误释放、`sched()` 状态、`walk()` 越界、`mappages()` remap、未持有 buffer sleeplock 的 `bwrite()/brelse()`、日志超限和 VirtIO 非零 completion status 会 panic；`releasesleep()` 不验证 owner，`end_op()` 不检查 `outstanding` 下溢，`bunpin()` 也不检查 `refcnt` 下溢。矩阵中的前置条件不能被理解成“违反后一定会被实现拒绝”。

## 2. 全局锁与上下文不变量

1. `acquire()` 先执行 `push_off()`，因此持有任意 spinlock 时本 hart 的 S-mode 中断关闭；`release()` 的 `pop_off()` 只在嵌套计数回到零且进入最外层前允许中断时恢复。
2. 可以睡眠的函数在实际可能阻塞的分支不得在调用点持有任意无关 spinlock；sleeplock 可以按既定锁序跨等待持有。`sleep(chan, lk)` 是交接机制：调用者持有且只额外持有条件 spinlock `lk`，入口的 `mycpu()->noff` 必须为 1；函数取得 `p->lock` 后释放 `lk`，再调度。竞争型 `acquiresleep()` 的入口 `noff` 必须为 0。`iput()` 在 `itable.lock` 下取得已由 `ref==1` 证明不会竞争的 inode sleeplock，是“可能调用但已证明不阻塞”的特例。
3. `sched()` 进入时只允许持有当前 `p->lock` 这一把 spinlock，`mycpu()->noff==1`，中断关闭，且进程状态已经不再是 `RUNNING`。因此 `yield()` 的调用点必须没有额外 spinlock 或裸 `push_off()` 层、`noff==0`，并且当前进程仍为 `RUNNING`；它可以跨 yield 继续持有符合锁序的 inode/buffer sleeplock。
4. buffer sleeplock、inode sleeplock可以跨磁盘等待持有；它们不能在外部中断上下文取得，也不能用来保护 IRQ handler 需要立即访问的状态。
5. 可能执行 `begin_op()` 或 `end_op()` 的路径，在可能等待容量或执行提交的分支前不能持有任何 spinlock，也不能持有会阻碍提交所需 buffer/inode 的 sleeplock；`begin_op()` 会在容量不足时睡眠，`end_op()` 也可能成为提交者并执行磁盘 I/O。
6. 当前日志、buffer cache 和文件系统只服务 `ROOTDEV`。`logheader` 不编码设备号，不能把同一个日志区间扩展为跨设备事务。

## 3. 同步与调度 helper

| helper | 调用前提 | 内部锁/状态 | 睡眠或切换 | 分配 | 日志区间 | 外部 IRQ |
|---|---|---|---|---|---|---|
| `push_off()/pop_off()` | hart 已完成 `tp` 初始化；严格配对 | 修改本 CPU `noff/intena` 和 `sstatus.SIE` | 否 | 否 | 无 | 可用，但不能错误恢复 trap 入口本应关闭的中断 |
| `acquire(lk)` | 当前 CPU 不已持有同一锁 | `push_off()` 后原子交换 `lk->locked`；记录 owner CPU | 否；竞争时自旋 | 否 | 无 | 可，设备 handler 正依赖此性质与进程上下文共享锁 |
| `release(lk)` | 当前 CPU 正持有 `lk` | 发布 fence、清 owner/locked、`pop_off()` | 否 | 否 | 无 | 可 |
| `acquiresleep(lk)` | 必须有当前进程；目标锁可能竞争时调用点不能持有其他 spinlock，且 `mycpu()->noff==0`；允许持有其他 sleeplock 但须满足锁序 | 短暂持有 `lk->lk`，忙时 `sleep(lk,&lk->lk)` | 是 | 否 | 无 | 不可 |
| `releasesleep(lk)` | 正常约定由 owner 进程调用；实现本身不 panic 校验 owner；释放后不得无锁访问由它保护的字段 | 持 `lk->lk` 清状态并 `wakeup(lk)` | 自身不睡眠 | 否 | 无 | 不可作为一般 IRQ API；它绕过 owner 检查，`holdingsleep()` 另需当前进程 |
| `sleep(chan, lk)` | 当前进程存在；条件 spinlock `lk` 已持有且不是 `p->lock`；入口 `mycpu()->noff==1`，不再持有其他 spinlock | 取得 `p->lock` 后释放 `lk`，设 `chan/SLEEPING`；醒来后反向交接 | 是，调用 `sched()` | 否 | 无 | 不可 |
| `wakeup(chan)` | 调用者按协议持有保护条件的锁；不能依赖唤醒即已运行 | 逐项取得其他进程的 `p->lock`，把匹配者改为 `RUNNABLE` | 否，但扫描 `NPROC` | 否 | 无 | 可；UART/VirtIO handler 都这样发布条件 |
| `sched()` | 当前 `p->lock` 是唯一 spinlock；状态非 `RUNNING`；中断关闭 | 保存进程 context，切到本 CPU scheduler context | 是 | 否 | 无 | 不可 |
| `swtch(old,new)` | 两个 context 存储都有效；`new` 中的 `ra/sp/s0..s11` 构成可恢复内核执行流；调用者自行满足锁和中断协议 | 无检查地保存 `old`、恢复 `new`；不保存 `gp/tp`、CSR 或 caller-saved GPR | 是；在另一执行流将 `old` 切回前不会从本次调用返回 | 否 | 无 | 不可作为 IRQ API |
| `yield()` | 当前进程存在且状态为 `RUNNING`；调用点没有额外 spinlock/`push_off()` 层且 `mycpu()->noff==0`；可持有符合锁序的 sleeplock | 取得 `p->lock`、设 `RUNNABLE`、调用 `sched()` | 是 | 否 | 无 | 外部 IRQ 不可；timer trap 只在 `myproc()!=0` 时调用 |
| `killed()/setkilled()` | `p` 槽在调用期间仍有稳定生命周期；调用者不能已持有同一 `p->lock` | 内部短暂取得 `p->lock` | 否 | 否 | 无 | 机械上不睡眠，但 handler 没有稳定任意 `p` 引用时不能使用 |

`releasesleep()` 和 `holdingsleep()` 的实现边界值得单列：错误进程释放锁不会被显式拒绝，递归 `acquiresleep()` 会睡在自己持有的锁上；`holdingsleep()` 在没有当前进程时不能安全调用，因为它会读取 `myproc()->pid`。释放 sleeplock 只撤销对受保护字段的互斥访问权，不自动撤销对象引用；持有稳定引用的调用者可以继续处理对象或重新加锁，`iunlockput()` 先解 inode 锁再 `iput()` 就是实例。正确性来自调用约定，而非 defensive check。`sleep()` 的 `noff==1` 不是形式上的偏好：它释放条件锁后必须让 `sched()` 看到只剩 `p->lock` 的那一层。

`push_off()` 是可嵌套计数而不是布尔开关。典型的 `sleep()` 交接序列是：条件锁取得一层，取得 `p->lock` 后变成两层，释放条件锁回到一层，`sched()` 返回后释放 `p->lock` 回到零，再重新取得条件锁。额外的裸 `push_off()` 会使 `sched()` 因 `noff!=1` panic。`panic()` 先设置 `panicking`；随后 `printk()`/`uartputc_sync()` 跳过各自的锁或 `push_off()`，并最终忙循环，因此 panic 路径不应被当成普通锁 API 的可恢复返回路径。

trap 入口由硬件清除 `sstatus.SIE`。因此 UART/VirtIO handler 的第一层 `acquire()` 通过 `push_off()` 记住的是“原本关闭”，最后一层 `release()` 不会在 handler 中途错误重开中断；handler 内嵌套的 `myproc()`、`wakeup()` 和 `uartputc_sync()` 也只会临时增加 `noff`。timer trap 在 `devintr()` 返回后可以于 `myproc()!=0` 时调用 `yield()`，因为正常内核代码持 spinlock 时 S-mode 中断已关闭，timer 不会在锁临界区打断它；外部设备 handler 本身没有这项调度许可。

## 4. 进程生命周期 helper

| helper | 调用前提与返回锁状态 | 内部锁/状态 | 可能睡眠 | 分配/释放 | 日志要求 | IRQ |
|---|---|---|---|---|---|---|
| `allocproc()` | 调用者不持 proc 锁；成功返回时新 `p->lock` 仍由调用者持有，状态为 `USED` | 扫描 `p->lock`，并用 `pid_lock`、`kmem.lock`；选中后一直持有该 `p->lock` | 否 | 分配 trapframe 和页表页；失败调用 `freeproc()` 回滚 | 无 | 不可作为 IRQ API；返回对象需要完整发布协议 |
| `freeproc(p)` | 必须持有 `p->lock`；只允许回滚尚未发布的构造失败槽，或由父进程持 `wait_lock -> p->lock` 回收 ZOMBIE | 保持 `p->lock`，释放页时短暂取得 `kmem.lock`；最后设 `UNUSED` | 否 | 释放 trapframe、页表和用户页，清空槽字段 | 无 | 不可 |
| `kfork()` | 当前进程上下文；调用点不持 proc/file/inode 锁 | 构造期间持 child `p->lock`，并在其内短暂取得 `ftable.lock`、`itable.lock`；释放 child 锁后用 `wait_lock` 发布 parent，最后再取 child 锁发布 `RUNNABLE` | 否，但可执行大量页复制 | 分配 child 槽和用户页；增加 fd/cwd 引用 | 无磁盘日志 | 不可 |
| `kwait(addr)` | 当前进程；不得持有其他锁 | 以 `wait_lock -> child p->lock` 扫描；睡眠时把 `wait_lock` 交接给当前 `p->lock` | 是，等待 child；copyout 还可能分配 lazy 页 | 回收 ZOMBIE 的全部页和 proc 槽 | 无 | 不可 |
| `kexit(status)` | 当前非 `initproc` 进程；不持有外部锁；永不返回 | 关闭资源时进入各子系统锁；最终按 `wait_lock -> p->lock` 设 `ZOMBIE`，释放前者后调用 `sched()` | 是，关闭资源后切换 scheduler | 关闭 fd/cwd、reparent；保留 zombie 页到 wait | 最后一个 `FD_INODE/FD_DEVICE` 引用的 `fileclose()` 自建区间；非最后引用和 pipe 不建；cwd 的 `iput()` 由 `kexit()` 外层区间包围 | 不可 |
| `kexec(path, argv)` | 当前进程；内核 argv 以 NULL 终止且非空参数最多 `MAXARG-1` | 日志锁、inode/buffer sleeplock 和 allocator 锁；新页表提交前私有 | 是，读取 inode/日志和 buffer | 分配临时新页表；提交前失败回滚新表 | 函数内部建立并结束读取 ELF 所需区间 | 不可 |

`allocproc()` 不重新初始化所有标量字段；正常复用依赖 `freeproc()`，而 `kill(0)` 可以污染 `UNUSED` 槽的 `killed`。因此“返回 `USED`”不等于所有字段都有构造函数式初值，详见[进程与调度](processes-and-scheduling.md)。

`kexec()` 自建日志区间不等于其所有失败都只有地址空间副作用。良构 executable 的 `[0,size)` 是 dense 的，`readi()` 只查询现有映射；损坏 inode 含 hole 时，同一 `readi()->bmap()` 会在这个合法 outstanding 区间内分配并登记磁盘块。该异常路径既可能超过 `MAXOPBLOCKS`，又没有 `readi()` 的 `iupdate()`/释放回滚，因此“新页表提交前私有”只约束 VM 所有权，不能推广为文件系统状态也完全回滚。

进程表还有两个刻意绕开普通运行期锁规则的入口：CPU 0 在任何槽可运行前由 `procinit()` 无锁写初始 `state=UNUSED`，而 `procdump()` 为避免死锁而无锁读取状态，仅提供可能不一致的诊断快照。它们不是新增运行期无锁访问的先例。

## 5. 页表、用户拷贝与物理页

| helper | 调用前提 | 锁/并发 | 睡眠 | 分配 | IRQ |
|---|---|---|---|---|---|
| `kalloc()/kfree()` | 页对齐和所有权必须正确；释放页不能仍被映射或引用 | 内部 `kmem.lock` | 否 | 取得/归还一页 | 机制上可在关中断环境运行，但当前设备 IRQ 不分配页，新增调用还需证明时延和所有权 |
| `walk(pt,va,alloc=0)` | `va<MAXVA`，否则 panic；页表生命周期稳定 | 调用者负责串行化页表结构 | 否 | 否 | 不作为设备 IRQ API |
| `walk(...,alloc=1)` | `va<MAXVA`；调用者独占结构修改；已有最终 PTE 可以合法返回 | 无页表锁；只补缺失的中间级 | 否 | 可能分配中间页表；后续层失败时先建的空层不回滚 | 不可从设备 IRQ 建映射 |
| `mappages()` | 调用者独占页表；`va/size` 页对齐、`size>0`，目标叶 PTE 均未映射，否则 panic | 无页表锁；逐页调用 `walk(...,1)` | 否 | 可分配中间页表；失败前已建的叶映射和空中间页表不回滚 | 不可从设备 IRQ 建映射 |
| `uvmalloc()` | 新页表尚未发布，或当前进程独占修改自己的页表；必须处理 0 | 无页表锁 | 否 | 分配多个叶页/中间页；失败回收本轮叶页，空中间页保留 | 不可 |
| `uvmcopy(old,new,sz)` | parent `old` 在复制期间稳定；child `new` 未发布；必须处理 -1 | 无页表锁 | 否 | 为已映射的 parent 页建立私有副本；失败回收已复制叶页，空中间页由调用者最终销毁 | 不可 |
| `uvmdealloc()` | 当前进程独占修改自己的页表，或目标已不可执行 | 无页表锁 | 否 | 只释放缩减范围内的叶页；不回收变空的中间页表 | 不可 |
| `uvmfree()` | 页表已从可执行身份撤下且不再并发访问；`sz` 必须覆盖其普通用户叶映射 | 无页表锁 | 否 | 先释放用户叶页，再由 `freewalk()` 递归释放全部中间页和根页；残留叶会 panic | 不可 |
| `copyinstr()` | `pt` 有效；目标内核 buffer 容量为 `max`；源页须同时有 `PTE_V`、`PTE_U` | 不取页表锁；不检查 `PTE_R` | 否 | 否；不会补 lazy 页；失败可已复制前缀且不保证 NUL | 仅进程上下文 |
| `copyin()/copyout()` | 允许进入 fallback 时要求 `pt==myproc()->pagetable`；`copyin` 逐页要求 `PTE_V|PTE_U`，`copyout` 另要求 `PTE_W` | 无页表锁，缺页时可能调用 `vmfault()` | 否 | 合法 lazy hole 可能分配页；失败不回滚已复制前缀或已物化页 | 仅进程上下文 |
| `vmfault(pt,va,read)` | 必须有当前进程；任何可能分配的调用要求 `pt==p->pagetable`、`va<p->sz` 且目标 PTE 尚无 `PTE_V`；`read` 参数当前未使用 | 当前进程独占自己的页表修改；新页一律映射 `PTE_R|PTE_W|PTE_U` | 否 | 一叶页加可能的中间页表；映射失败释放叶页但可留下空中间页 | 用户 page-fault 或当前页表的用户拷贝路径可用；设备 IRQ 不可 |

`vmfault()` 接受一个 `pagetable` 参数做初始 `ismapped()`，但用当前 `p->sz` 判界，最终又调用 `mappages(p->pagetable,...)`。因此传入临时非当前页表的调用者只能依赖“所有要复制的页已经映射，绝不会进入 lazy fallback”；`kexec()` 向 eager 建好的新栈写数据正满足这一前提。

若把 `copyin()/copyout(newpt,...)` 泛化到含 hole 的临时页表，结果取决于当前页表而不是稳定失败：copy helper 传给 `vmfault()` 的向下取整页首 `va0` 不低于当前 `p->sz` 时返回 0；原始字节地址略高于 break、但仍与 break 位于同一页时不会被这个判断拒绝。当前页表同地址已经映射时，错误的 `mappages()` 会触发 remap panic；当前页表也有 hole 时，新页会被错误装入当前页表。后一种情况下 `copyin()` 直接使用返回的物理地址，可能从错误的新零页复制并返回成功；`copyout()` 随后仍查询 `newpt`，可能因无 W 返回 `-1`，也可能在缺少中间页表时解引用空 PTE 指针并触发内核 fault。所有分支都没有可靠填充 `newpt`，所以“非当前页表不得触发 fallback”是调用前提，而不是普通错误处理建议。

三个 copy helper 都是逐页推进而非事务接口。后续页失败时，`copyin()` 的内核目标、`copyout()` 的用户目标或 `copyinstr()` 的字符串缓冲区都可能已有前缀；前两者先前物化的 lazy 页也继续存在。调用者只能在成功返回后把 `copyinstr()` 目标当作 NUL 终止字符串，并且不能从任一 `-1` 推断内存和页表完全未变。

这些函数没有页表锁。同一进程不会在两个 hart 同时执行、内核构建页表在发布前私有，这两个更高层不变量替代了锁；若引入共享地址空间或多线程，必须增加同步和跨 hart TLB shootdown。

## 6. Buffer、日志和 inode helper

| helper | 调用前提/返回状态 | 内部锁 | 睡眠 | 分配/释放 | 日志区间 | IRQ |
|---|---|---|---|---|---|---|
| `bread(dev,bno)` | 调用前不持有会阻止等待的 spinlock；返回 `b->lock` sleeplock 已持有，必须 `brelse()` | `bcache.lock`、`b->lock`、VirtIO lock | 是，cache 命中也可能等 sleeplock | 取得固定 cache 槽的 ref；无空闲 buffer 时 panic，不 `kalloc()` | 单纯读取不要求；若上层可能修改则先有区间 | 不可 |
| `bwrite(b)` | 当前进程持有 `b->lock` | VirtIO 内部 lock | 是，等待设备完成 | 固定 descriptor；不分配内存/磁盘块 | commit/recovery 可直接用；普通元数据修改应走 `log_write()` | 不可 |
| `brelse(b)` | 当前进程持有 `b->lock`，调用后不得再访问 buffer 内容 | 释放 sleeplock，再取 `bcache.lock` | 自身不睡眠 | 释放一个 cache ref，必要时移入 MRU 端 | 无 | 不可；owner 由 pid 判定 |
| `bpin()/bunpin()` | buffer identity 在调用期间稳定；计数严格配对 | `bcache.lock` | 否 | 增减固定 buffer 的 ref；`bunpin()` 不检查下溢 | 由日志内部管理 | 可执行但当前设备 IRQ 不应改变日志 pin |
| `begin_op()` | 当前进程；不持 inode/buffer sleeplock或其他 spinlock；若进入等待分支则 `mycpu()->noff==0`；必须最终配对 | `log.lock` | 是，等待容量/commit | 只增加预留计数，不分配 block | 建立当前调用链预算 | 不可 |
| `log_write(b)` | 调用者仍持有修改后 buffer 的 sleeplock；当前调用链已有区间 | `log.lock -> bcache.lock`（仅首次登记时 pin） | 否 | 固定 log header 槽和 buffer ref；不分配 block | 必需；只看全局 outstanding 是实现弱点 | 不可作为 handler API |
| `end_op()` | 当前调用链有一个未结束区间；之前的 inode/buffer 锁和其他可能阻塞的锁已释放 | `log.lock`；最后者执行 commit | 是，可能做完整磁盘提交 | 不分配；提交后解除 pin | 结束区间；实现不检查 `outstanding` 下溢 | 不可 |
| `ilock(ip)` | 调用者持有稳定 `ref>0` 的 inode 引用；不已持同一 sleeplock | inode sleeplock；首次从磁盘装入 | 是 | 只使用固定 inode/buffer cache | 读取本身不要求，但调用者通常已建立上层区间 | 不可 |
| `iunlock(ip)` | 当前进程持 inode sleeplock | 释放 sleeplock | 否 | 否 | 无 | 不可 |
| `iupdate(ip)` | 持 inode sleeplock，且当前调用链已有日志区间 | bread/log/brelse | 是 | 固定 buffer/log 槽 | 必需 | 不可 |
| `iput(ip)` | 持稳定引用但不持 inode sleeplock；所有调用必须位于区间，因为最后引用可能 truncate；`ref==1` 分支取得 inode sleeplock 时依赖它已证明无竞争 | itable spinlock，必要时 inode sleeplock和磁盘 buffer | 是；已证明的 `ref==1` 取锁本身不阻塞，随后 truncate/磁盘操作会睡眠 | 最后无链接引用会释放 inode 及数据/间接磁盘块 | 必需 | 不可 |
| `readi(ip,...)` | 持 inode sleeplock；正常无 hole 文件只读 | buffer sleeplock；用户目标可能 `copyout` | 是 | 用户目标 lazy hole 可能分配物理页；损坏文件 hole 还可能分配磁盘块 | 正常读取无；损坏 hole 会经 `bmap()` 意外修改，是当前信任边界缺陷 | 不可 |
| `writei(ip,...)` | 持 inode sleeplock；调用链已有区间；范围已由上层分片 | buffer/inode，用户源可能 `copyin` | 是 | 可能分配数据/间接磁盘块；用户源 lazy hole 可能分配物理页 | 必需 | 不可 |
| `namei()/nameiparent()` | 相对路径需要当前进程和稳定 `cwd` 引用；绝对路径从 `ROOTINO` 开始；路径必须是内核字符串 | 逐 inode sleeplock和引用锁 | 是 | 正常路径只取得固定 cache 引用；推进/失败时的最后 `iput()` 可能回收无链接 inode 和磁盘块 | 一般必需，因为路径失败/收尾释放 `iput()` 可能回收；`namei("/")`（以及无 component 的绝对路径）只做 `iget()`，冷启动例外不需要当前进程或区间；`nameiparent()` 的收尾仍可能 `iput()` | 不可 |

`iget()/idup()` 只操作 inode cache identity/ref，不取得 inode sleeplock，也不读取磁盘；`ilock()` 才使磁盘字段有效。持有 `ip->lock` 不能替代内存 `ref`，反之亦然。

## 7. File、pipe 和设备入口

| helper | 前置条件与所有权 | 内部锁 | 睡眠 | 分配 | 日志 | IRQ |
|---|---|---|---|---|---|---|
| `filealloc()/filedup()` | file table 已初始化；`filedup` 要求稳定正 ref | `ftable.lock` | 否 | 固定表槽/引用 | 无 | 不作为设备 IRQ API |
| `fileclose(f)` | 调用者拥有一个 ref；调用后该 ref 失效 | `ftable.lock`；按类型再进入 pipe/inode 路径 | 可能，最后 inode/device ref 会提交，pipe close 只唤醒 | 可能最终释放 pipe 页 | 最后 inode/device ref 内部建立区间；其他情况无 | 不可 |
| `fileread()/filewrite()` | 调用期间 `struct file` ref 稳定；不得预持 inode/pipe/device lock | 按类型进入 `pipe.lock`、inode sleeplock 或设备锁 | 是 | 用户拷贝可补 lazy 页；写可能分配 block | inode write 内部分片建区间 | 不可 |
| `pipealloc()` | 调用点可处理失败并关闭部分资源 | 短暂使用 `ftable.lock`；初始化 pipe lock | 否 | 两个 file 槽和一物理页 | 无 | 不可 |
| `piperead()/pipewrite()` | 稳定 pipe/file ref；用户地址属于当前进程 | `pipe.lock`；`killed()` 还短暂取得 `p->lock` | 是，空/满时 sleep | copyin/out 可能补 lazy 页 | 无 | 不可 |
| `pipeclose()` | 调用者移交一个端点所有权，不能再使用 | `pipe.lock` | 否 | 两端都关时 `kfree` | 无 | 不可作为 IRQ API |
| `uartwrite()` | 普通进程写路径；不持 spinlock | `tx_lock` | 是，等待 THRE 中断清 `tx_busy` | 否 | 无 | 明确不可 |
| `uartputc_sync()` | 可接受无 timeout 的 MMIO busy-wait；panic 路径有特殊规则（`panicking` 时跳过 `push_off()`，`panicked` 后永久自旋） | 无 | 否，但可永久自旋 | 否 | 无 | 可，console 回显使用；不提供跨 CPU 消息原子性 |
| `uartintr()/consoleintr()` | PLIC 已 claim UART，trap 入口中断关闭 | `tx_lock`、`cons.lock`；`C('P')` 分支还会经 `procdump()` 取 `pr.lock` | 不睡眠；可能长时间轮询/扫描 | 否 | 无 | 是，专用 handler |
| `virtio_disk_rw(b,rw)` | 当前进程持有 buffer sleeplock；请求期间 buffer identity/data 稳定 | `vdisk_lock` | 是，等待 `b->disk` 清零 | descriptor 来自固定队列 | 无独立日志要求 | 不可 |
| `virtio_disk_intr()` | PLIC 已 claim IRQ 1，设备 interrupt status 待确认 | `vdisk_lock`；标记 `b->disk=0` 并 `wakeup(b)`，请求线程醒来后才释放 descriptor chain | 否 | 否 | 不修改日志 | 是，专用 handler；非零 completion status 会 `panic()` |

当前 `struct file` 没有独立 offset lock。fork 后父子共享同一个 file object，普通 inode 的 `fileread()/filewrite()` 在持 inode sleeplock 时更新 `f->off`，从而借 inode 锁串行化该对象的 offset；pipe/device 不使用该 offset。若以后允许同一 file object 指向不共享 inode 锁的 seekable backend，需要增加 file 级锁。

## 8. 常见非法组合

| 非法组合 | 直接后果 |
|---|---|
| 持 inode sleeplock 后调用 `begin_op()` | 日志容量不足时睡眠并长期占有 inode，可能阻止让现有 group 完成的线程 |
| 在 UART/VirtIO handler 调用 `bread()`、`uartwrite()` 或 `acquiresleep()` | handler 尝试 `sleep()`，没有合法的阻塞协议，破坏 trap/调度不变量 |
| 未建立自己的区间、看到全局 outstanding 后调用 `log_write()` | 消耗别人的预留，可能使 group 超过 `MAXOPBLOCKS/LOGBLOCKS` |
| `brelse()` 后继续读写 `b->data` | buffer 可立即被 LRU 复用于另一 block，形成身份混淆和数据破坏 |
| 只有 inode 指针但无 ref 时调用 `ilock()` | 槽可能已被复用为不同 `(dev,inum)`，锁不能保护身份生命周期 |
| 对临时含 hole 页表调用会进入 fallback 的 `copyin()/copyout()` | `vmfault()` 可能拒绝、remap panic 或错误映射当前 `p->pagetable`；不会可靠填充临时页表 |
| 持多个 spinlock 进入 `sched()` | `noff!=1` 触发 panic，或把其他 CPU 永久挡在无人释放的锁外 |
| 在额外 `push_off()` 或持 spinlock 时调用 `yield()` | `yield()` 先无条件把状态设为 `RUNNABLE`；取得自身锁后 `noff` 仍大于 1，`sched()` 立即 `panic("sched locks")` |
| 对状态并非 `RUNNING` 的当前进程调用 `yield()` | `yield()` 静默覆盖原来的 `SLEEPING/ZOMBIE/...` 为 `RUNNABLE`；若其余锁条件正常，`sched()` 的状态检查不会发现这次破坏 |
| 在额外 `push_off()` 下让竞争型 `acquiresleep()` 进入等待 | `sleep()` 交接后 `sched()` 看到多于一层 `noff`，触发 panic |
| 在额外 `push_off()` 下调用 `sleep(chan,lk)` | 释放条件锁后仍有额外禁中断层，`sched()` 触发 `sched locks` |
| 在持有同一 `p->lock` 时调用 `killed()/setkilled()` | 内部 `acquire(&p->lock)` 检测递归持有并 `panic("acquire")` |
| 在未证明 `ref==1` 分支外持有 `itable.lock` 调用会阻塞的 inode helper | 若 helper 睡眠，`sched()` 看到额外 spinlock；`iput()` 的特殊分支仅因 `ref==1` 保证 inode sleeplock 不竞争 |
| 第一次 `malloc()` 前直接调用用户态 `free()` | 用户 allocator 的 `freep` 哨兵环尚未建立，发生空指针解引用；这是用户态对应的上下文契约 |

## 9. 审阅新增调用点的步骤

1. 从调用点反向列出当前进程是否存在、当前特权/中断状态以及已经持有的每把锁。
2. 沿被调函数所有失败分支查找 `sleep()`、`sched()`、`bread()`、`acquiresleep()`、`begin_op()/end_op()` 和 `kalloc()`，不能只看正常路径。
3. 若触及磁盘修改，用[资源上界](resource-bounds.md)计算新增 unique home blocks，并确认区间从获取 inode 锁前开始、在释放锁后结束。
4. 若从 IRQ 调用，要求整个可达调用图无睡眠、无进程专属 owner 检查，并给出 MMIO/共享内存的确认和发布顺序。
5. 若操作页表，说明谁保证它未在另一 hart 并发使用，以及何时需要本 hart 或跨 hart TLB 刷新。
6. 对每个返回值写明资源仍由谁拥有；尤其区分“返回失败但留下已登记修改”和“完整回滚”。

这份矩阵是当前调用图的审阅索引，不是由类型系统强制的效果标注。任何新增线程、共享地址空间、多设备文件系统、异步 I/O 或可抢占内核设计都会改变其中多项结论，届时必须同步修改实现和文档。
