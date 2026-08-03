# 可伸缩性、扫描成本与 I/O 放大

xv6 的目标是展示机制，而不是随 CPU、进程、内存和设备队列线性扩展。本文量化当前实现中的扫描、共享锁、稀疏地址空间成本和存储放大。所有数值来自源码常量或算法推导；除非明确标注实验结果，表中不是实测性能。

## 1. 分析边界

当前编译期容量包括 `NCPU=8`、`NPROC=64`、`NFILE=100`、`NINODE=50`、`NBUF=30`、`NOFILE=16`、`FSSIZE=2000` 和 VirtIO queue `NUM=8`；Makefile 默认以 `CPUS=3`、128 MiB RAM 启动 QEMU。固定上限使最坏扫描在教学配置中有限，但不改变热点的增长阶数。`NCPU` 是 hart id/数组容量上限，不等于默认运行 hart 数。

记号：

- `P=NPROC`，`F=NFILE`，`I=NINODE`，`B=NBUF`；
- `V=PGROUNDUP(p->sz)/PGSIZE`，逻辑用户页数，包含 lazy holes；
- `T` 为实际存在的页表页数；每张 Sv39 页表有 512 项；
- `D` 为目录的 dirent 数；`K` 为一次提交的 unique home block 数；
- `Q=8` 为 VirtIO descriptor 总数。

## 2. 进程表与调度

| 路径 | 单次成本 | 当前绝对扫描上界 | 共享状态 |
|---|---:|---:|---|
| `scheduler()` 一轮 | `O(P)` | 64 slots/hart/round | 每槽 `p->lock` |
| `wakeup(chan)` | `O(P)` | 64 slots | 每槽 `p->lock` |
| `kkill(pid)` | `O(P)` | 64 slots | 每槽 `p->lock` |
| `allocproc()` | `O(P)` | 64 slots | 每槽 `p->lock` + `pid_lock` |
| `kwait()` 一次检查 | `O(P)` | 64 slots | 全局 `wait_lock` + child locks |
| `reparent(p)` | `O(P)` | 64 slots | 全局 `wait_lock` |

每个 hart 独立从 `proc[0]` 开始扫描，没有 per-CPU run queue、优先级或 affinity。默认 3 个 hart 已会反复争用相同低索引槽的锁；把 `CPUS` 提高到编译期上限 8 时竞争进一步增加。`RUNNABLE` 进程分布和槽顺序会影响等待时间。

`wakeup()` 在持调用者条件锁时扫描整个表。它可能从 UART/VirtIO/timer 中断路径执行，因此一次字符发布或 I/O 完成可引入 64 次候选检查；中断关闭时间还包含 spinlock 获取等待。多个 waiters 同通道会全部变 RUNNABLE，形成 thundering herd。

单次 `reparent` 是 `O(P)`。若一棵进程关系在一段运行中产生 `E=O(P)` 次退出，每次都全表扫描，则总 reparent 工作是 `O(E*P)=O(P^2)`；最坏 64x64 次 parent 比较。`kwait` 被反复无效唤醒时也可重复全表扫描，不能仅按最终 child 数计算。

把进程置 `RUNNABLE` 不发送 reschedule IPI；idle hart 只能等本地 timer/设备事件离开 `wfi`。这是调度延迟而非扫描吞吐问题，但扩展 CPU 数时同样会限制负载分布。

## 3. 固定对象表

| 分配/查找 | 实现 | 最坏成本 | 耗尽结果 |
|---|---|---:|---|
| `filealloc` | 扫 `ftable.file[NFILE]` | `O(F)=100` | 返回 0 |
| `fdalloc` | 扫当前 `ofile[NOFILE]` | `O(NOFILE)=16` | 返回 -1 |
| `iget` | 一遍找已有、一遍记空槽 | `O(I)=50` | panic |
| `bget` | 找 cache hit，否则从 LRU 尾找 `refcnt==0` | `O(B)=30` | panic |
| `allocproc` | 扫 proc slots | `O(P)=64` | 返回 0 |
| `kalloc/kfree` | freelist 头操作 | `O(1)` | `kalloc` 返回 0 |

这些表分别由全局或每槽锁保护。增加容量可推迟耗尽，却线性加重 miss/scan 路径；简单把 `NINODE/NBUF/NFILE` 放大不是可伸缩设计。

`iget` 在 `itable.lock` 下扫描，命中后只增加 ref；真正磁盘加载再取 inode sleeplock。大量不同 inode 会同时增加表扫描与 sleeplock竞争。`bget` 的全局 `bcache.lock` 串行化 cache lookup、ref 更新和 victim 选择，磁盘完成后仍需它处理引用。

## 4. 调用链中的乘法效应

复杂度会跨层相乘。例如一次 UART newline 可能：

```text
consoleintr holds cons.lock
  -> wakeup scans P slots
     -> each candidate acquires p.lock
```

一次 pipe read/write 每个字节在持 `pipe.lock` 时调用 `copyin/out`；helper 至少 walk 用户页表。对 `n` 字节，成本近似 `O(n * page-walk)`，而不是按页批量复制。锁持有时间也随 n 增长，直到满/空睡眠发生交接。

console write 虽先按最多 32 字节从用户区批量 copyin，`uartwrite` 仍按字节取得 TX 协议锁、写 THR、等待 THRE 中断。长输出的调度/中断次数和锁交接近似随字节数线性增长。

## 5. 稀疏地址空间

lazy allocation 让物理页数与逻辑 `p->sz` 解耦，但若算法仍按 `[0,sz)` 逐页扫描，时间成本没有稀疏化：

| 操作 | 当前算法 | 时间 | 备注 |
|---|---|---:|---|
| `uvmcopy`/fork | 对每个 VA 调 `walk`，hole 跳过 | `O(V)` | 复制物化页之外仍扫描全部 hole |
| `uvmunmap`/shrink | 对范围逐页 `walk` | `O(number of logical pages)` | 只清叶，不剪空中间表 |
| `uvmfree` | 先按 `[0,sz)` unmap，再 `freewalk` | `O(V+512T)` | 巨大稀疏 sz 可令退出/reap极慢 |
| `freewalk` | 每张实际页表扫描 512 PTE | `O(512T)` | 与物化页表结构相关 |
| 单次 `vmfault` | 分配数据页，`walk(alloc=1)` | `O(3)` levels | 最多消耗数据页 + 两张中间表页 |

shrink 不回收空的中间页表。进程可反复触及不同 2 MiB/1 GiB 子树后缩回，逻辑 size 很小却保留 `T`，直到最终 `freewalk`。这既是存活期内存放大，也是后续 walk/cache 压力。

当前 `TRAPFRAME` 附近允许近 `2^38` 的用户 VA。若 `sz=TRAPFRAME`，低区逻辑页数约 `2^26-2=67,108,862`；即使只物化最后一页，最终 `uvmfree` 仍会进行约 6711 万次页迭代。这是源码级推导，不代表默认 workload 会走到该边界。

改进方向应从“遍历逻辑区间”变为“遍历实际页表叶子”，并在 unmap 时剪空分支；但必须保留 TRAMPOLINE/TRAPFRAME 特殊映射、hole 语义和失败回滚。

## 6. 文件系统扫描

### 6.1 inode、bitmap、目录

- `ialloc` 顺序读磁盘 dinode blocks，最坏 `O(sb.ninodes)`；当前镜像 200 dinodes。
- `balloc` 从 bitmap 起点顺序找 0 bit，最坏 `O(sb.size)` bits；没有 free-space hint。
- `bfree` 定位是 `O(1)`，但必须读写对应 bitmap buffer。
- `dirlookup` 顺序扫描 `[0,dp->size)`，成本 `O(D)`；固定 14 字节名字没有索引。
- `dirlink` 先调用 `dirlookup` 检查重复，再扫描空槽，最坏接近两遍目录，即 `O(2D)`，最后可能扩目录。
- 路径有 `m` 个分量时，成本是各级目录扫描之和，并伴随 inode cache lookup/锁/I/O。

目录删除的 `isdirempty` 从第三个 dirent 起线性扫描。大目录上的 lookup/create/unlink 会争用同一个 inode sleeplock，因而不仅是 I/O 次数问题。

### 6.2 buffer cache 与日志

buffer cache 只有 30 个条目且使用单个全局元数据锁；热点命中仍需全表线性搜索。日志会 pin 已登记 home buffer，减少可替换集合；多个 cache miss caller 可各持 buffer 并在 VirtIO descriptor 上睡眠。`NBUF==LOGBLOCKS` 本身不是系统活性证明。

`begin_op()` 在全局 `log.lock` 下按 `outstanding` 预留最坏日志空间。持续重叠 operation 可让 `outstanding` 长期不归零，延后已经返回的更新 commit；没有 epoch、定时 commit 或公平队列。

## 7. 日志与磁盘 I/O 放大

一次含 `K>0` 个 unique home blocks 的 commit 执行：

```text
K writes: copy home buffers to log data blocks
1 write : non-zero log header (commit point)
K writes: install log blocks to home locations
1 write : clear log header
--------------------------------------------
2K + 2 xv6 block writes
```

每个 `BSIZE=1024` request 又覆盖两个 512B sector。若把用户有效数据字节记为 `U`，仅提交阶段的 block-write amplification 是 `(2K+2)*1024/U`；实际还要加 bitmap、inode、目录等元数据 home blocks和读 miss。log absorption 可让同一 home block 的多次修改只计一次 K，group commit 又可由多个 syscall 分摊固定的两个 header write。

普通缓存命中不产生设备 read；cache miss 和 log 安装中的 `bread` 是否真正读盘取决于 buffer 是否仍驻留/pin。不能仅从函数调用次数等同设备请求次数。

单次大 `filewrite` 被切成受 `MAXOPBLOCKS` 限制的多个事务，每一块分段都会重复 header 固定成本。增大 chunk 可降低固定放大，却必须重新证明日志 unique-block上界和 buffer活性。

## 8. VirtIO 并发上界

queue 有 8 个 descriptors，每个请求固定占 3 个：header、data、status。

```text
max in-flight = floor(8 / 3) = 2
unused when full = 2 descriptors
```

第三个请求睡在 descriptor 可用条件上。`vdisk_lock` 串行化 descriptor 分配、avail ring发布和 used ring回收；设备可并行处理至多两笔，但提交/完成的软件路径仍经过同一锁。一个 xv6 buffer 在 I/O 期间保持 `disk=1` 和引用，caller 睡眠；完成中断清标志、唤醒等待者。

加大 queue 不能单独解决吞吐：还要增加 descriptor allocator、buffer cache 容量/查找结构、请求合并和设备 feature 协商。当前没有多 queue、indirect descriptors、event index 或调度器。

## 9. 主要锁竞争图

| 热点锁 | 竞争者 | 临界区内高成本动作 |
|---|---|---|
| `bcache.lock` | 所有文件系统 I/O | 线性 lookup/victim scan |
| `log.lock` | 所有写事务 | 容量判断、登记、commit 状态交接 |
| `itable.lock` | 所有 inode refs | 50 槽扫描、ref/回收交接 |
| `ftable.lock` | file alloc/dup/close | 100 槽 scan 或 ref update |
| `vdisk_lock` | 所有 block I/O + IRQ | descriptor/ring 操作、wakeup |
| `kmem.lock` | 所有页分配/释放 | freelist 头；短但全局 |
| `wait_lock` | fork/exit/wait/reparent | 可包含全进程表扫描 |
| `cons.lock` | UART RX 与 console readers | 编辑、同步回显、wakeup scan |

spinlock 还关闭当前 hart 中断。临界区变长不仅增加其他 CPU 自旋，还增加本 hart interrupt latency；因此在锁内调用 `wakeup(O(P))` 的成本跨越调度和设备实时性。

## 10. 可复现实验设计

不要只打印 wall-clock 总时间。建议加入仅测试构建启用的 per-hart counters/histograms：

1. `scheduler_slots_scanned`、`wakeup_slots_scanned`、`runnable_to_run_cycles`，比较 `CPUS=1/2/4/8` 和不同 runnable 数。
2. `uvm_pages_visited`、`freewalk_ptes_visited`、`page_table_pages_live`，对固定物化页数逐步增加逻辑 `sz`，验证 `O(V)` 斜率。
3. `bget_entries_examined`、cache hit/miss、pin high-water，区分缓存查找和设备时间。
4. `log_unique_blocks`、group size、每次 commit 的 block reads/writes，核对 `2K+2`，并记录 absorption。
5. VirtIO in-flight high-water、descriptor wait cycles、completion batch size，验证上界为 2。
6. pipe/console 每用户字节的 copy helper、sleep/wakeup、UART interrupt次数。

每组实验固定 git commit、编译参数、`CPUS`、`fs.img` 哈希、QEMU版本、虚拟/宿主计时源，预热与重复次数。counter 本身会改变锁和时序，应先校验无 instrumentation 的功能结果，再把数据解释为趋势而非生产基准。

## 11. 优化时必须保留的正确性

- per-CPU run queue 仍要保证同一进程不被双重运行，并处理 wakeup/exit/迁移所有权；
- hashed inode/buffer cache 仍要保证 `(dev,inum)` / `(dev,blockno)` 唯一对象；
- page-table稀疏遍历仍要处理 lazy holes和高位特殊映射；
- larger/multi-queue VirtIO 仍需精确 descriptor与buffer DMA所有权；
- batch pipe copy 仍需定义 partial bad-address语义和多 writer交错；
- delayed/grouped log commit 仍需维持 header commit point、pin和容量证明。

性能结构可以替换，不能用“压力测试没挂”替代这些不变量。全局证明索引见[全局不变量](../correctness/global-invariants.md)，资源耗尽结果见[资源失败矩阵](../reference/resource-failure-matrix.md)。
