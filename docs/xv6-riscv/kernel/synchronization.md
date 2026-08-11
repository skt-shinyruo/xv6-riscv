# 同步、锁与等待通道

本文说明 xv6 在多 hart、内核抢占和设备中断环境中的同步模型。核心不是 API 名称，而是锁与中断的绑定、`sleep/wakeup` 的条件锁协议，以及各子系统必须遵守的锁顺序。

## 1. 源码地图

- `kernel/spinlock.h`、`kernel/spinlock.c`：spinlock、owner 记录、嵌套关中断；
- `kernel/sleeplock.h`、`kernel/sleeplock.c`：允许持有者睡眠的长临界区锁；
- `kernel/proc.c:sleep()`、`wakeup()`、`sched()`：等待通道和调度交接；
- `kernel/proc.h:struct cpu`：`noff/intena` 保存关中断嵌套状态。

具体锁的业务含义分别在进程、文件系统、存储和设备文档中说明。

## 2. 为什么 spinlock 与关中断绑定

`acquire(lk)` 首先调用 `push_off()`，然后原子交换 `lk->locked`。关闭当前 CPU 的 supervisor 中断解决两个问题：

1. 当前 CPU 持锁时若中断处理程序再次尝试同一把锁，会在本 CPU 永久自旋；
2. `mycpu()`/`lk->cpu` 依赖当前内核线程不会在读取过程中因 timer interrupt 被调度到另一 CPU。

这不阻止其他 CPU 并行执行；其他 CPU 仍靠原子 `amoswap` 竞争锁。

## 3. 获取和释放的内存顺序

`acquire()` 使用 `__atomic_exchange_n(..., __ATOMIC_ACQUIRE)`：

- 返回旧值为 0 的 CPU 获得锁；
- acquire 语义阻止临界区读写被移动到获取锁之前；
- 获锁后设置 `lk->cpu = mycpu()`，供递归获取检测和调试。

`release()` 先清空 `lk->cpu`，再用 `__atomic_store_n(..., __ATOMIC_RELEASE)` 清 `locked`：

- release 语义保证临界区写入在其他 CPU 获锁前可见；
- 检测到当前 CPU并未持锁会 `panic`；
- 最后调用 `pop_off()`，可能恢复中断。

`holding()` 只在中断已关闭时可靠，因为它比较 `lk->cpu` 与当前 CPU。

## 4. `push_off()/pop_off()` 的嵌套协议

每个 `struct cpu` 保存：

- `noff`：当前嵌套关闭中断层数；
- `intena`：最外层 `push_off()` 之前中断是否开启。

第一次 `push_off()` 记录旧中断状态，之后只增加 `noff`。`pop_off()` 只在 `noff` 降到零且 `intena` 为真时重新开中断。因此：

```text
初始开中断 -> push -> push -> pop -> pop -> 恢复开中断
初始关中断 -> push -> pop                 -> 保持关中断
```

不匹配的 `pop_off()` 或在 `pop_off()` 时中断已经被其他代码打开都会 panic。这能暴露锁/中断层次损坏。

## 5. Spinlock 的适用边界

适合：

- 极短的内存状态更新；
- 中断处理程序与进程上下文共享的数据；
- 不能睡眠的上下文；
- 保护另一个睡眠协议的元数据。

不适合：

- 等待磁盘、UART、pipe 数据或另一个长期持有者；
- 持锁执行会睡眠的用户拷贝上层路径或复杂文件系统 I/O。当前 `copyin()/copyout()` 自身不睡眠，但可能为 lazy 页调用 `vmfault()` 并在业务锁内短暂取得 `kmem.lock`，还可能只复制前缀就失败；
- 跨用户态返回或跨任意长时间保存。

持有任意 spinlock 时 `noff > 0`，`sched()` 要求 `noff == 1`。调用 `sleep(chan, lk)` 时允许仍持有那一把保护等待条件的 `lk`；`sleep()` 先取得 `p->lock`，再原子释放 `lk`，只有在进入 `sched()` 的时刻才必须剩下 `p->lock`和 `noff == 1`。调用者不能额外携带其他 spinlock。

## 6. Sleeplock 的实现

`struct sleeplock` 包含状态 `locked`、内部 spinlock `lk` 和调试用持有者 pid。`acquiresleep()`：

1. 获取内部 `lk`；
2. 如果 `locked` 为真，执行 `sleep(lk, &lk->lk)`；
3. 醒来时已经重新获得内部 `lk`，重新检查条件；
4. 设置 `locked=1` 和持有者 pid；
5. 释放内部 `lk`。

真正持有 sleeplock 时不持有内部 spinlock，因此中断可以开启，进程也可以在磁盘 I/O 中睡眠。

`releasesleep()` 清状态、`wakeup(lk)` 并释放内部锁。所有等待者都会变为 `RUNNABLE`，最终只有一个在重新检查条件后取得锁。

Sleeplock 只能在有当前进程的上下文使用，因为它记录 `myproc()->pid` 并可能睡眠；中断处理程序不能使用。

实现没有 defensive owner enforcement：`releasesleep()` 不先检查调用者 pid，错误进程也能清锁；同一进程递归调用 `acquiresleep()` 会等待自己释放并永久睡眠。`holdingsleep()` 只是查询当前 pid 是否匹配。正确调用者、非递归和稳定对象生命周期都是 API 前置条件，而不是锁结构自行保证。

## 7. `sleep(chan, lk)` 的原子交接

调用者持有保护“等待条件”的 spinlock `lk`。`sleep()` 必须避免以下丢失唤醒：条件检查后、进程登记为 `SLEEPING` 前，生产者完成条件并调用 `wakeup()`。

实际顺序是：

```text
caller holds lk
  -> acquire p->lock
  -> release lk
  -> p->chan = chan
  -> p->state = SLEEPING
  -> sched()                 # 带着 p->lock 交给 scheduler
  ... wakeup acquires p->lock and sets RUNNABLE ...
  -> scheduler resumes process, still holds p->lock
  -> clear p->chan
  -> release p->lock
  -> reacquire lk
  -> return to caller
```

完整证明需要生产者遵守同一协议：它必须持有同一个条件锁 `lk` 改变等待谓词，并在该锁仍持有时调用 `wakeup(chan)`；`wakeup` 检查目标状态时又取得目标 `p->lock`。在睡眠者释放 `lk` 前已经持有 `p->lock`，所以生产者不可能在“谓词已成立但睡眠状态尚未发布”的窗口内同时越过两把锁。若生产者无锁改变谓词，即使 `sleep()` 自身顺序正确也仍可能丢失唤醒。

调用者返回后重新持有 `lk`，必须在循环中重新检查条件；唤醒不是条件本身，也可能有多个等待者竞争同一资源。

## 8. `wakeup(chan)`

`wakeup()` 线性扫描进程表，对除当前进程外的每个槽获取 `p->lock`。若状态为 `SLEEPING` 且 `p->chan == chan`，改为 `RUNNABLE`。

除 kill 这种明确的取消唤醒外，业务生产者应在保护谓词的条件锁内先更新状态、再调用 `wakeup()`。唤醒只发布调度资格，不保证 waiter 立即运行；waiter 恢复后必须在同一条件锁下用 `while` 重查谓词。可重复的正反例见[丢失唤醒实验](../labs/lost-wakeup.md)。

kill 正是条件锁证明之外的例外，而不是自动安全的替代协议。`kkill()` 只取得目标 `p->lock`，不取得 console、pipe、ticks 或 `wait_lock`；若目标在持条件锁检查完 `killed==0`、但尚未由 `sleep()` 取得 `p->lock` 时被 kill，killer 看到的仍是 `RUNNING`，只会置位。目标随后仍可发布 `SLEEPING`，直到真实条件变化或第二次 kill 才醒来。要实现无此窗口的可取消等待，必须让取消方参与条件锁，或让 waiter 在已经封闭睡眠发布窗口后再次检查取消标志。

等待通道只是内核指针的相等性标识，不被解引用。常见通道包括：

| 通道 | 条件锁 | 条件 |
|---|---|---|
| `&ticks` | `tickslock` | timer tick 增加 |
| `&cons.r` | `cons.lock` | console 提交了输入 |
| `&pi->nread` | `pi->lock` | pipe 中有数据或写端关闭 |
| `&pi->nwrite` | `pi->lock` | pipe 有空位或读端关闭 |
| `b` | `vdisk_lock` | VirtIO 将 `b->disk` 清零 |
| `&disk.free[0]` | `vdisk_lock` | 有至少三个 descriptor 可用 |
| `&log` | `log.lock` | commit 完成或预留空间释放 |
| `p` | `wait_lock` | 某个子进程进入 ZOMBIE |

## 9. 调度器的锁交接

进程调用 `sched()` 时持有自己的 `p->lock`，`swtch()` 回调度器后，调度器继续持有同一把锁。调度器完成 `c->proc=0` 等收尾后才释放它。

反向切换时，调度器先持有候选 `p->lock`、设置 `RUNNING`，再 `swtch()` 到进程。进程首次从 `forkret()` 或后来从 `sched()` 返回时继承这把锁，并在合适位置释放。

这种“锁跨 context switch 转移给另一条执行流”的方式是 xv6 中最特殊的锁所有权模式。锁保护的不只是字段赋值，还保护进程内核栈：调度器必须确认旧 CPU 已完全停止使用该栈，另一个 CPU 才能运行该进程。

## 10. 全局锁清单

| 锁 | 保护内容 | 可在中断使用 | 持有期间可睡眠 |
|---|---|---:|---:|
| `kmem.lock` | 物理页 freelist | 机制上可；当前设备 IRQ 不使用 | 否 |
| `pid_lock` | `nextpid` | 否 | 否 |
| `wait_lock` | `p->parent`、wait/reparent 顺序 | 否 | 仅通过 `sleep(...,&wait_lock)` |
| `p->lock` | 进程状态、chan、killed、pid、xstate | 调度/trap 路径使用 | 作为 `sched` 交接锁 |
| `tickslock` | 全局 `ticks` | timer 中断使用 | 仅通过 sleep 交接 |
| `ftable.lock` | `struct file.ref` 和槽分配 | 否 | 否 |
| `itable.lock` | inode cache 身份和 `ref` | 否 | `iput` 特殊切换到 inode sleeplock |
| `ip->lock` | inode 有效内容和文件数据操作 | 否 | 是，sleeplock |
| `bcache.lock` | buffer 身份、refcnt、LRU 链 | 否 | 否 |
| `b->lock` | 单个 block 数据 | 否 | 是，sleeplock |
| `log.lock` | outstanding、committing、内存 log header | 否 | 仅通过 sleep 交接 |
| `vdisk_lock` | descriptor/ring/in-flight 请求 | 设备中断使用 | 进程路径通过 sleep 交接 |
| `cons.lock` | console 输入环形缓冲 | UART 中断使用 | read 路径通过 sleep 交接 |
| `tx_lock` | UART `tx_busy` | UART 中断使用 | write 路径通过 sleep 交接 |
| `pi->lock` | pipe 数据、计数器和端点状态 | 否 | 读写路径通过 sleep 交接 |
| `pr.lock` | 一次 `printk` 输出 | 可从中断获取 | 否 |

## 11. 重要锁顺序

- `wait_lock` 必须先于任意相关 `p->lock`；`proc.c` 明确把它作为父子关系的全局顺序锁。
- scheduler 只持有一个 `p->lock`；`sched()` 对 `noff == 1` 的断言强制这一规则。
- inode 正常路径不要同时持有两个无固定顺序的 inode sleeplock；路径遍历先获取 next 的引用，再释放当前 inode。
- `itable.lock` 管理 inode 身份/ref；inode sleeplock 管理内容。`iput()` 只有在 `ref==1` 保证无人持有 inode 锁时才从前者切换到后者。
- buffer cache 先在 `bcache.lock` 下固定 buffer 身份/ref，再释放全局锁并等待 `b->lock`，避免持全局锁睡眠。
- `end_op()` 在 `log.lock` 外执行 `commit()`，因为 commit 会做磁盘 I/O并睡眠。
- lazy 用户拷贝可在已持有 `p->lock`、pipe spinlock 或 inode/buffer sleeplock 的路径中进入 `vmfault() -> kalloc() -> kmem.lock`。因而当前实现存在“业务对象锁先于 `kmem.lock`”的实际嵌套方向；新的物理页分配路径不得反向持 `kmem.lock` 再等待这些对象锁。

### 11.1 当前源码的跨模块依赖图

下表记录的是当前调用图中确实可达的“左锁仍持有时获取右锁”关系。`condition -> p->lock` 在 `sleep()` 中只维持到条件锁被释放，在 `wakeup()` 中则逐个短暂取得目标进程锁；它仍是死锁分析必须保留的真实边。

| 已持有 | 随后可能获取 | 入口和原因 |
|---|---|---|
| `wait_lock` | child/current `p->lock` | `kwait()` 扫 child，`kexit()` 发布 ZOMBIE；这是父子关系的强制全局顺序 |
| 新建或待回收 `p->lock` | `pid_lock` | `allocproc()` 持槽锁调用 `allocpid()` |
| `p->lock` | `kmem.lock` | `allocproc/kfork/freeproc/kwait` 分配、复制或释放 trapframe/页表；wait 的 copyout 也可补 lazy 页 |
| child `p->lock` | `ftable.lock`、`itable.lock` | `kfork()` 在 child 尚为 `USED` 且锁仍持有时执行 `filedup()`、`idup()` |
| `itable.lock` | `ip->lock` 内部 spinlock | `iput()` 在 `ref==1,nlink==0` 时取得 sleeplock；`ref==1` 保证不会实际等待，随后先释放 itable 再做磁盘工作 |
| `pi->lock` | current/other `p->lock` | pipe 读写调用 `killed()`，以及 `sleep/wakeup` 条件交接 |
| `cons.lock` | current/other `p->lock` | console read 的 `killed/sleep` 与 input handler 的 `wakeup` |
| `tx_lock` | waiter `p->lock` | UART write 的 `sleep` 和 THRE handler 的 `wakeup` |
| `vdisk_lock` | waiter `p->lock` | descriptor/buffer 等待的 `sleep/wakeup` |
| `log.lock` | waiter `p->lock` | `begin_op/end_op` 的容量/commit sleep-wakeup |
| `tickslock` | waiter `p->lock` | `sys_pause()` 睡眠与 timer handler 唤醒 |
| inode/buffer sleeplock、`pi->lock` 或 `cons.lock` | `kmem.lock` | `copyin/copyout -> vmfault -> kalloc` 为合法 lazy 用户页补页 |
| `cons.lock` | `pr.lock` | Ctrl-P 的 `consoleintr() -> procdump() -> printk()`；正常字符回显不取 `pr.lock` |
| `log.lock` | `bcache.lock` | `log_write()` 首次登记时调用 `bpin()`；normal commit 本身在 log.lock 外执行 |
| `bcache.lock` | 无 buffer sleeplock嵌套 | `bget()` 固定 identity/ref 后先释放全局锁，再 `acquiresleep(b->lock)`，刻意切断这条危险边 |

可以用下列压缩图做反向检查：

```text
wait_lock ------> p->lock ------> pid_lock / kmem.lock / ftable.lock / itable.lock
condition lock -> p->lock
itable.lock ----> ip->lock(internal; guaranteed non-blocking transition)
inode/buffer/pipe/console lock -> kmem.lock
cons.lock ------> pr.lock
log.lock -------> bcache.lock
```

图中的 `p->lock` 可能属于不同 proc 槽，不能据此任意把两个进程锁嵌套；scheduler 仍只允许当前一个 `p->lock`。也不能从“当前没有反向边”推导未来调用安全：例如新增 `kmem.lock -> pipe lock` 或 `pr.lock -> cons.lock` 都会与现有方向形成环。审阅新增调用时应从完整可达调用图重新生成边，而不是只看函数体中的直接 `acquire()`。

不存在一张能覆盖所有未来修改的静态锁序图。新增跨模块调用时必须逐层确认是否持锁、是否可能睡眠、是否可能由中断重入。

## 12. 常见错误

- 获取 spinlock 后直接调用会睡眠的函数，导致 `sched locks` panic 或系统死锁；
- 检查条件后先释放条件锁、再设置 `SLEEPING`，造成丢失唤醒；
- 用 `if` 而不是 `while` 检查睡眠条件；
- 在中断上下文获取 sleeplock；
- 未成对调用 `push_off/pop_off`，破坏后续 CPU-local 状态；
- 违反 `wait_lock -> p->lock` 顺序，在 exit/wait/reparent 并发时死锁；
- 在持有 `p->lock` 之外修改 `state/chan/killed`；
- 把 `wakeup()` 当作唤醒单个 waiter，实际它唤醒所有匹配进程。

## 13. 验证

- `usertests preempt` 验证 timer 抢占和 pipe 唤醒能在忙循环中推进。
- `usertests exitwait`、`reparent`、`twochildren`、`forkfork` 暴露进程锁顺序和父子关系竞态。
- `usertests pipe1` 验证 pipe 满/空时的睡眠唤醒。
- `usertests manywrites` 和 `logstress` 压力覆盖 buffer、日志和 VirtIO 锁交接。
- `grind` 长时间随机组合进程、内存、文件和 pipe 操作，适合发现低概率死锁。
- 调试 `sched locks` panic 时首先检查 `mycpu()->noff` 和当前持有的 spinlock，而不是只看 panic 点。
