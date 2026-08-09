# 同步与失败问题

本组问题用于检查锁与中断的边界、CPU 与设备的内存可见性，以及固定资源耗尽时并不统一的失败语义。

## 问题

1. **SYNC-01** spinlock 为什么既要原子操作又要关闭本 hart 中断？两者分别解决什么问题？
2. **SYNC-02** acquire/release 内存序能保证 CPU 临界区可见性，为什么不自动等价于 VirtIO DMA 的发布顺序？
3. **SYNC-03** 外部设备中断为什么不能调用 `bread()`、`acquiresleep()` 或 `begin_op()`？即使当前恰好不阻塞也不行吗？
4. **SYNC-04** 为什么持有额外 spinlock 或额外 `push_off()` 时调用 `yield()` 会触发 `sched locks`？
5. **SYNC-05** 进程槽、物理页、fd、inode cache、buffer、日志和 VirtIO descriptor 耗尽时，为什么有的返回 `-1`、有的睡眠、有的 kill、有的 panic？
6. **SYNC-06** `NINODE=50` 和磁盘 `NINODES=200` 分别是什么？为什么前者耗尽会 panic，后者通常只让创建失败？
7. **SYNC-07** 为什么 `NBUF == LOGBLOCKS` 不能证明 buffer 数量足够？
8. **SYNC-08** 提高 `NPROC` 为什么不仅是扩大数组，还会增加永久内核栈、扫描延迟、内存消耗和最大资源引用数？
9. **SYNC-09** 一个并发测试连续运行一万次没有失败，为什么仍不能证明没有竞态？怎样为丢失唤醒设计确定性的失败 oracle？

## 源码入口

- [`kernel/spinlock.c`](../../kernel/spinlock.c)
- [`kernel/sleeplock.c`](../../kernel/sleeplock.c)
- [`kernel/proc.c`](../../kernel/proc.c)
- [`kernel/param.h`](../../kernel/param.h)
- [`kernel/pipe.c`](../../kernel/pipe.c)
- [`kernel/console.c`](../../kernel/console.c)
- [`kernel/virtio_disk.c`](../../kernel/virtio_disk.c)

## 配套文档

- [同步与锁](../xv6-riscv/kernel/synchronization.md)
- [调用上下文契约](../xv6-riscv/kernel/call-context-contracts.md)
- [资源失败矩阵](../xv6-riscv/reference/resource-failure-matrix.md)
- [全局正确性不变量](../xv6-riscv/correctness/global-invariants.md)
- [可重复故障注入](../xv6-riscv/verification/fault-injection.md)
- [确定性丢失唤醒实验](../xv6-riscv/labs/lost-wakeup.md)
