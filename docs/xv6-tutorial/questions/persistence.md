# Buffer cache、日志与事务问题

学习目标：从一次修改型 filesystem operation 建立 cache -> log -> commit -> home -> clear 的
端到端地图，再深入 buffer identity、reservation、pinning、并发与持久化边界。硬前置是
[文件系统命名、inode 与数据路径](../core/filesystem.md)。每题都从 pinned source symbol 和可观察
状态开始，不以函数名释义、一次 PASS 或 timeout 作答。

## 高层总览

### PERSIST-01 一次 filesystem write 跨过哪些 ownership 与持久化边界？

从 `kernel/file.c:filewrite`、`kernel/fs.c:writei`、`kernel/bio.c:bread`、
`kernel/log.c:log_write` 和 `kernel/virtio_disk.c:virtio_disk_rw` 开始，列出 caller、buffer cache、
in-memory log header、on-disk log、home block 与设备请求各自拥有的状态和交接点。

## 流程骨架

### PERSIST-02 一个 home block 怎样从 cached dirty 走到 cleared？

从 `begin_op()` breadth-first 追踪 `bread()`、修改、`log_write()`、`end_op()`、`write_log()`、
`write_head()`、`install_trans()` 和第二次 `write_head()`。先给出全部阶段和可见状态，不先深入
某把锁；区分 operation return、transaction commit 和 log clear。

### PERSIST-03 一个未缓存 read 怎样建立并释放 buffer identity？

从 `kernel/bio.c:bget` 追到 hit/miss、victim retag、sleeplock acquire、`bread()` fill、
`virtio_disk_rw()` completion 与 `brelse()`。记录 `(dev, blockno)`、slot、`valid/disk/refcnt` 和
LRU 位置何时改变。

## 局部深入

### PERSIST-04 one-buffer-per-block 在 `bget()` 的哪段原子成立？

比较 hit scan 与 miss/victim scan 怎样共享 `bcache.lock`，以及为何必须在 release 前完成
identity publish 和 `refcnt++`。构造两个 actor 同时请求同一未缓存 block 的状态表，说明仅靠
buffer sleeplock 为何不能阻止 duplicate identity。

### PERSIST-05 `brelse()` 的顺序怎样限制 use-after-release 与 eviction？

沿 `brelse()` 的 holdingsleep check、sleeplock release、`bcache.lock`、ref decrement 和 LRU move
阅读。说明 caller 在哪一步失去使用 `b->data` 的权利，以及 candidate 使用 partition-local
eviction 时仍必须保留哪些 victim eligibility 与 generation 不变量。

### PERSIST-06 ordinary ref、waiter、sleeplock、pin 和 disk owner 为什么不能合并？

比较 `bget()`、`brelse()`、`bpin()`、`bunpin()` 与 `virtio_disk_rw()` 对 `refcnt`、buffer lock 和
`b->disk` 的修改。画出同一 block 被一个 actor 持有、另一个等待、log pin 保留且设备短暂拥有时
的 ledger，并指出 raw `refcnt` 无法独立分解哪些来源。

### PERSIST-07 `begin_op()` 的 reservation 公式如何决定 sleep？

从 `kernel/param.h:MAXOPBLOCKS/LOGBLOCKS` 与 `kernel/log.c:begin_op` 推导 admission predicate。
分别计算 `lh.n=0` 时第四个 operation、以及 `lh.n=1/outstanding=2` 时第三个 operation；追踪 sleep
channel、wakeup 来源和重新检查 while condition 的必要性。为什么 `begin_op()` 必须早于 inode
等可能跨 sleep 持有的锁？反序会怎样让已有 reservation 无法到达 `end_op()`？

### PERSIST-08 `end_op()` 返回为何不总等于本 operation 已 committed？

比较非最后一个与最后一个 outstanding operation 的 `end_op()` 分支。记录 `outstanding`、
`committing`、`do_commit`、wakeup 与 commit 调用的位置，解释多个 system call 的更新怎样组成一个
group transaction，以及哪个 caller 等到 clear 完成。

### PERSIST-09 `log_write()` 的 absorption 与 pinning 怎样协作？

让同一 transaction 两次修改同一个 buffer，逐项跟踪 `log.lh.block[]` scan、`lh.n`、`bpin()`、
最终 payload 和 `install_trans()` 中的 `bunpin()`。说明为什么重复 entry 或过早 unpin 会分别破坏
容量和数据版本。

### PERSIST-10 真正的 commit point 是 submit 还是 completion？

沿 `write_log()`、第一次 `write_head()`、`virtio_disk_rw()` sleep/wakeup 和 `bwrite()` return
定位非零 header write completion。比较 payload complete、header submit、header complete、home
install 与 zero-header clear，说明每个时点允许称为什么状态。

## 横切机制

### PERSIST-11 `NBUF == LOGBLOCKS` 为什么不能证明不会耗尽 buffer？

从当前两个常量都等于 30 出发，列出 home buffers、log destination buffers、ordinary refs、
waiters 和 pins 的重叠生命周期。用 `bget: no buffers` 的 exact precondition 说明数值相等没有包含
哪些 owner/resource relation。

### PERSIST-12 parallel cache 如何同时证明 identity、collision 与实际并行？

设计三个确定性场景：same block、different blocks in same partition、different partitions。
分别给出稳定 block/partition discovery、gate 能否 sleep 或必须双 hart spin、期望 slot/generation、
lock acquire order、内容与 cleanup；指出 stress/timeout 为什么不能替代这些事件。

### PERSIST-13 raw event 如何变成独立 correctness oracle？

从 `scenarios.json` 的 closed event schema 与 point capabilities 开始，说明 guest、QEMU adapter、
replay adapter、host reducer 各负责什么。列出 host 必须拒绝的 seq/schema/gate/ledger mutation，并
说明 instrumentation 对时序和非对抗 candidate review 的限制。

### PERSIST-14 #16 的 `R` 与 #17 的 recovery claim 在哪里分开？

比较无 crash 的 orderly completion、QEMU request completion、QEMU 退出后的 host-visible private
image，以及尚未执行的 named crash/synthetic tear/replay/idempotent restart/offline fsck。给出本
单元可接受的 R oracle 和必须留作 `crash-recovery-and-offline-consistency` 黑盒的结论。

## 全流程串联

### PERSIST-15 如何复核一个 transaction 和一个 parallel-cache candidate？

只复用 PERSIST-01..14 的模型：先重建同一 home block 两次修改的 admission、cache、absorption、
payload、commit、install、clear；再串起 capacity wait、same-block、collision、different-partition
和 full-cache case。把稳定 identity、event order、S/F/B/C/R、allowed side effects、regressions、
资源 ledger、cleanup 与 #17 边界放进一个报告包。

答案与证据标准见[配套答案](answers/persistence.md)。
