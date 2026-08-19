# 可扩展性与证据综合问题

本问题链由 `core.evidence-synthesis` 拥有。先建立 workload/capacity 地图，再沿
`MAXOPBLOCKS 10 -> 11` 的假设变更追踪派生状态，最后把三类 evidence domain 汇成一份可重算报告。
所有答案都必须回到 pinned source、verified artifact 和明确 gap；问题中的变更不得实际应用。

## 高层总览

### SYNTH-00 为什么 capacity vector 不能直接预测 scalability？

从 `NPROC`、`PHYSTOP`、`NOFILE/NFILE/NINODE`、`NBUF/LOGBLOCKS`、`NUM`、`FSSIZE/MAXFILE`
建立 `W -> K` 地图。对 runnable processes、unique dirty blocks 和 in-flight disk requests，分别指出
capacity、owner、锁/per-hart state/queue/cache/storage serialization 怎样共同决定 progress。

### SYNTH-01 三类 evidence domain 怎样共用 S/F/B/C/R 而不互相冒充？

从 scheduling/device、global-invariants/filesystem/persistence、recovery 三组 verified review 各选一条
raw relation。它们分别支持哪个 bounded claim；为什么字母集合不能被当成 proof score？

## 流程骨架

### SYNTH-02 `MAXOPBLOCKS` 怎样从定义传播到 image 与 runtime？

先 breadth-first 追踪 `kernel/param.h:MAXOPBLOCKS -> LOGBLOCKS/NBUF -> kernel/log.c:begin_op/log_write ->
kernel/file.c:filewrite -> mkfs/mkfs.c:nlog -> fs.img -> tests`。每段传递的是容量、整数公式、owner、
layout 还是 oracle requirement？

### SYNTH-03 一条 source change 怎样得到 mandatory/related/gap 三类 reverse index？

以 `kernel/log.c:commit()` 或 `MAXOPBLOCKS` 变化为起点，使用 fault traceability record 的
source-owner-invariant-test-gap 结构。哪些 persistence/recovery oracle 必跑，哪些 filesystem/user tests 只是
related regression？`kernel/defs.h` 的 declaration surface 是否改变，`user/grind.c:go()` 与
`user/stressfs.c:main()` 能提供什么 F evidence；哪些 physical-durability claim 仍无 oracle？

## 局部深入

### SYNTH-04 `MAXOPBLOCKS 10 -> 11` 实际改变和不改变什么？

静态重算 `LOGBLOCKS`、`NBUF`、`mkfs nlog`、metadata offsets、empty-log admission、`filewrite()` chunk、
`user/usertests.c:BUFSZ`。逐项注明 C 整数除法与 `FSSIZE` 固定造成的结果，并解释旧 image 是否可复用。

### SYNTH-05 process 扩容为什么同时触及 per-hart 与全局状态？

从 `proc_mapstacks()`、`proc[NPROC]`、`scheduler()`、`wakeup()`、`p->lock` 与 `PHYSTOP` 追踪
`NPROC` 增加后的永久 pages、每 hart scan、file refs 和可能的下一个瓶颈。哪些是确定的操作数关系，
哪些必须由 workload measurement 建立？

### SYNTH-06 buffer/log 扩容为什么不等于 storage throughput 扩容？

沿 `bget -> bpin -> log_write -> end_op -> commit -> virtio_disk_rw -> virtio_disk_intr -> free_chain`
追踪 unique dirty block。比较 `bcache.lock`、buffer sleeplock、`log.lock`、`disk.vdisk_lock`、`NUM=8`
和 home install；说明更大 `NBUF/LOGBLOCKS` 会把压力移向哪里。

### SYNTH-07 哪些 exact outcomes 必须在 capacity report 中保留？

比较 process/page/fd/file/inode/buffer/log/VirtIO exhaustion 的 return、sleep、kill、panic 与 partial result。
若 hypothetical change 没有 live trigger，报告怎样区分 source prediction、历史 observation 与 unverified gap？

## 横切机制

### SYNTH-08 一份 reproducibility record 为什么必须包含 image 状态？

针对 `MAXOPBLOCKS 10 -> 11`，填写 baseline、environment、configuration、image、trigger、expectation、
observation、limits。解释 `nlog`/metadata layout 改变后，为什么旧 `fs.img` 和历史 QEMU report 只能作为
原配置 evidence，不能成为新配置结果。

### SYNTH-09 如何审查历史报告、fresh self/static 与 fresh dynamic 的 provenance？

从 persistence/recovery review 选择一份绑定旧 runner 或 external candidate 的报告，列出 artifact hashes、
产生配置与 current compatibility check。哪些措辞会把 historical dynamic 冒充 fresh evidence？

## 全流程串联

### SYNTH-10 怎样完成一次不越界的扩容影响审查？

用 `MAXOPBLOCKS 10 -> 11` 串联 workload、source definitions、derived capacity、owner/wait/serialization、
image layout、mandatory tests/resources、three evidence domains、S/F/B/C/R、cleanup 和 remaining gaps。
最后写出当前 evidence 支持的最强一句结论，并删除 formal proof、fairness、all interleavings、benchmark
speedup 与 physical durability 主张。
