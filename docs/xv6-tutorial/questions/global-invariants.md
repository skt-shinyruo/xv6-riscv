# 全局不变量与资源边界问题

本问题链由 `core.global-invariants` 拥有。先从跨子系统 owner 图进入，再沿资源取得/转移/
回滚/释放进入局部路径，最后综合容量修改；每题都要引用 stable source symbol 和同一份
`FD_ROLLBACK` 报告，不能用一次 `PASS` 代替关系。

## 高层总览

### INVARIANT-00 一次 filesystem write 使用了哪些不同的 owner？

从 `kernel/file.c:filewrite`、`kernel/fs.c:writei`、`kernel/bio.c:bread`、
`kernel/log.c:log_write`、`kernel/virtio_disk.c:virtio_disk_rw` 和
`kernel/log.c:commit` 开始，画出 process/fd/file/inode/buffer/log/device/persistence 的对象图。
哪些边是 identity、reference、lock、reservation、in-flight 或 durable bytes？

### INVARIANT-01 为什么一个总计数不能表示全局不变量？

用 `proc[NPROC]`、physical page、`p->ofile[]`、`struct file.ref`、inode `ref/nlink` 和
buffer `(dev,blockno)/refcnt/pin` 比较 count invariant 与 identity/ownership invariant。
给出至少一个“总数正确但 owner 错误”的反例，并说明稳定观察边界。

## 控制流与状态转换

### INVARIANT-02 `sys_pipe()` 怎样发布 ownership，并在失败时逐步回退？

先 breadth-first 追踪成功的
`sys_pipe -> pipealloc -> filealloc x2 -> kalloc -> fdalloc x2 -> copyout -> return 0`，标出两个
fd 何时才可被 caller 使用。再对每个 failure edge 写出已取得资源、是否已发布、cleanup owner
和允许保留的 user-memory 副作用；最后深入 `bad` branch。

### INVARIANT-03 从 `allocproc()` 到 `kwait()`，slot 与 page 的释放责任何时移交？

用 `USED/RUNNABLE/RUNNING/ZOMBIE/UNUSED` 标出 child 发布点、scheduler handoff、exit 保留项和
parent reclaim。`allocproc()` 的 trapframe/page-table failure 与已发布 child 的 exit 为什么不能
使用同一 cleanup 时机？

### INVARIANT-04 log reservation 怎样跨越 buffer 与 device ownership？

从 `begin_op -> log_write -> end_op -> commit -> virtio_disk_rw -> virtio_disk_intr` 追踪
reservation、pinned home buffer、descriptor chain、completion 与 header/home bytes。
指出每次 sleep 的谓词/lock/producer，以及 IRQ 为什么不能直接完成 transaction cleanup。

## 局部边界

### INVARIANT-05（原 SYNC-05）耗尽为什么有五种不同结果？

对 process slot、physical page、fd、file、inode cache、buffer、log、VirtIO descriptor 分别给出
source anchor 和 exact result：return `-1`、sleep、kill、panic 或 partial result。哪些结果由
底层 allocator 决定，哪些由 caller 转换？失败后哪些 owner 必须重新枚举？

### INVARIANT-06 interrupt token 与 device request 是同一个 owner 吗？

从 `devintr -> plic_claim -> uartintr/virtio_disk_intr -> plic_complete` 比较 PLIC source token、
device status、request buffer 和 sleeping process。说明 ACK、complete、wakeup、descriptor reclaim
各转移什么，哪些动作绝不能在 IRQ context 中执行。

### INVARIANT-07 `FD_ROLLBACK` 怎样成为精确 exhaustion oracle？

从 `kernel/param.h:NOFILE` 与 raw `IO BASE/FD_ROLLBACK/AFTER` 独立复算
`16 - 3 == 13`。为什么还必须验证 files/refs/pipes/procs/free、关闭 duplicates、第二轮 BASE、
focused/quick/full 和 process cleanup？这个 trigger 在第几个 `fdalloc()` 失败；它没有动态覆盖
哪个 partially-installed fd branch，以及哪些其他容量？

## 跨切面与综合

### INVARIANT-08（原 SYNC-08）提高 `NPROC` 会改变哪些隐含上界？

从 `proc_mapstacks()`、`proc[NPROC]`、`scheduler()`/`wakeup()` 的扫描、`ofile[NOFILE]` 和
fork inheritance 出发，计算永久 kernel stack 页、scan length 与最大 file-reference pressure
怎样联动。为什么只改 `NPROC` 可能把瓶颈移动到 pages 或 `NFILE`？

### INVARIANT-09 提高 `MAXOPBLOCKS` 时要重做哪张证明表？

从 `MAXOPBLOCKS -> LOGBLOCKS/NBUF`、`begin_op()` inequality、`filewrite()` chunk、buffer pins、
VirtIO queue 和 image log layout 建立影响链。为每条边选择 S/F/B/C/R 证据，并明确哪些只能由
静态推理或未来 fault injection 建立，不能由现有回归证明。

### INVARIANT-10 如何审查一个“所有测试通过”的资源修改？

任选 `NPROC`、`NOFILE`、`NBUF` 或 `NUM` 的一个改动，提交完整
`identity/capacity/owner/acquire/transfer/rollback/release/exhaustion/post_failure` 表，列出 trigger、
raw observation、cleanup 和 remaining gaps。最后给出可以支持的最强结论，并删除所有 formal
proof、fairness、all-interleavings 或 physical-durability 越界主张。
