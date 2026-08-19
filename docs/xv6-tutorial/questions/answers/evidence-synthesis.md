# 可扩展性与证据综合答案与证据标准

本页是 review 标准，不替代学习者的 impact report。所有数值以 pinned baseline 为准；
`MAXOPBLOCKS 10 -> 11` 只是未应用的 source review。

## SYNTH-00

capacity vector 只给数组、地址范围或 descriptor 数。progress 还受 owner 发布/回收、锁竞争、每 hart
`scheduler()` 扫描、wait predicate、buffer pin、log commit 与 storage completion 约束。合格地图至少把
runnable process 连到 `NPROC/PHYSTOP/p->lock`，dirty blocks 连到 `NBUF/LOGBLOCKS/log.lock`，disk
requests 连到 `NUM/disk.vdisk_lock/IRQ`；不能从一个更大的常量直接推出吞吐提高。

## SYNTH-01

三域都使用相同字母，但 claim 不同：named scheduler order 是 C，`FD_ROLLBACK` exhaustion/ledger 是 B，
normal regressions 是 F，source/owner chain 是 S，crash image/offline/second boot 是 R。组合表示同一有限
结论有多种证据，不表示覆盖率或 proof score；每行仍要保留未触发路径、all-interleavings、公平性与
physical durability gap。

## SYNTH-02

完整骨架是：`MAXOPBLOCKS` 派生 `LOGBLOCKS/NBUF`；`begin_op()` 用它做 aggregate admission，
`log_write()` 用 `LOGBLOCKS` 限制/pin unique home blocks，`filewrite()` 用它算 transaction chunk；
`mkfs` 用 `LOGBLOCKS+1` 写 superblock layout；tests 用同一 header 改变 buffer size/边界输入。runtime
必须使用由该配置重新构建的 image。任何跳过 `mkfs`/image 的链都不完整。

## SYNTH-03

`commit()` 或 log capacity 变化的 mandatory oracle 是 transaction payload/header/home/clear、admission、
cache/pin 与 recovery crash points；filesystem focused/full tests 是 related regression。真实介质掉电、
arbitrary tear、performance/fairness 没有当前 oracle。`kernel/defs.h` prototype 不随本常量变化；`grind`
的随机 syscall mix 与 `stressfs` 的并行读写只提供 broad F regression，没有 stable trigger/ledger/crash
relation。反向索引必须给 record/source relation，不能因为 test 文本出现“log”就升级为 mandatory。

## SYNTH-04

精确表应为：`LOGBLOCKS/NBUF 30 -> 33`；`nlog 31 -> 34`，metadata/data boundary 移动且 data blocks
减少 3；空日志最多接纳的 operations 仍是 3；`filewrite()` chunk 因 `(7/2)==3` 仍为 3072 bytes；
`BUFSZ 12*BSIZE -> 13*BSIZE`。`NPROC/PHYSTOP/NUM/MAXFILE/FSSIZE` 不直接改变。旧 image layout 与新
kernel expectation 不构成可信 evidence，必须重建后才可动态验收。

## SYNTH-05

`proc_mapstacks()` 对每个 slot 预分配 kernel stack，增加 `NPROC` 会确定地增加永久 page 需求；每个 hart
的 `scheduler()` 和 `wakeup()` 线性扫描上界也增加。file refs 可随更多 process/fork 增加，但只有新的
`filealloc()` 才消耗 distinct `NFILE` slots。具体 wall-clock cost、contention 与 throughput 必须由指定
workload 测量，不能由循环上界推导。

## SYNTH-06

buffer/log 扩大允许更多 identities/pins，但最后一个 `end_op()` 仍串行进入 `commit()`；每个 disk request
仍需三个 descriptors，`NUM=8` 时第三笔等待。`bcache.lock`、buffer sleeplock、`log.lock` 和
`disk.vdisk_lock` 保护不同阶段，IRQ 只发布 completion，requester reclaim chain。压力可能移到 physical
pages、descriptor queue、commit/home writes；没有 benchmark 就不能声称 throughput 增加。

## SYNTH-07

报告要分别写出 return `-1`、sleep、kill、panic、partial result，并注明谁清理/重试。未运行 hypothetical
配置时，派生数值和 branch 是 S prediction；verified 原配置 report 是 historical observation；新配置的
live exhaustion、concurrency 和 recovery 是 gap。三者不得放进同一 “PASS” 栏。

## SYNTH-08

可复现记录至少绑定 baseline/tutorial commit、toolchain/QEMU/CPUS/memory、所有相关常量、image path/hash
或明确 `not built`、trigger/command、expected relation、raw observation/digest 与 limits。这里 image 必须写
`not built; hypothetical change not applied`。`nlog` 改变会移动 superblock metadata offsets，因此旧 image
只证明原配置；不能通过重命名或复制获得新配置 evidence。

## SYNTH-09

artifact row 应把 fixture/scenario/runner/candidate/report hashes 和产生日期/配置作为同一 provenance。
current self/static 只能证明 parser/source/apply/build compatibility；它不能刷新绑定旧 runner 的 QEMU
transcript，也不能替代 recovery external candidate。合格措辞显式使用 historical/current/fresh，且不把
不同 run 的 digest 混成一个 bundle。

## SYNTH-10

合格综合先声明 hypothetical change 与 workload，再列 changed/unchanged source relation、owner/wait/
serialization、image invalidation、mandatory/related/gap reverse index；随后各引用一个 concurrency、
ownership、recovery verified artifact，分栏写 S/F/B/C/R 和 cleanup。当前最强结论只能是：pinned source
静态确定该变更的派生容量、布局与不变项，并确定新配置必须重跑的 bounded oracles；没有建立新配置的
runtime scalability、recovery compatibility 或 physical durability。
