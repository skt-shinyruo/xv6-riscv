# 同步与失败问题

本组问题用于检查锁与中断的边界、CPU 与设备的内存可见性，以及固定资源耗尽时并不统一的失败语义。

## 问题

1. **SYNC-01** 已迁移到[调度与同步问题 `SCHED-04`](../xv6-tutorial/questions/scheduling-and-synchronization.md#sched-04原-sync-01)。此处只保留兼容入口。
2. **SYNC-02** 已迁移到[设备中断与 VirtIO 队列问题 `DEVICE-08`](../xv6-tutorial/questions/device-io.md#device-08-两个-fenceavail-idx-和-notify-建立了什么没建立什么)。此处只保留兼容入口。
3. **SYNC-03** 已迁移到[调度与同步问题 `SCHED-09`](../xv6-tutorial/questions/scheduling-and-synchronization.md#sched-09原-sync-03)。此处只保留兼容入口。
4. **SYNC-04** 已迁移到[调度与同步问题 `SCHED-02`](../xv6-tutorial/questions/scheduling-and-synchronization.md#sched-02原-proc-02)。此处只保留兼容入口。
5. **SYNC-05** 进程槽、物理页、fd、inode cache、buffer、日志和 VirtIO descriptor 耗尽时，为什么有的返回 `-1`、有的睡眠、有的 kill、有的 panic？
6. **SYNC-06** 已迁移到[文件系统问题 `FILESYS-06`](../xv6-tutorial/questions/filesystem.md#filesys-06-struct-dinode-struct-inode-与两个-inode-上界为什么不能合并)。此处只保留兼容入口。
7. **SYNC-07** 已迁移到[持久化问题 `PERSIST-11`](../xv6-tutorial/questions/persistence.md#persist-11-nbuf--logblocks-为什么不能证明不会耗尽-buffer)。此处只保留兼容入口。
8. **SYNC-08** 提高 `NPROC` 为什么不仅是扩大数组，还会增加永久内核栈、扫描延迟、内存消耗和最大资源引用数？
9. **SYNC-09** 已迁移到[调度与同步问题 `SCHED-07`](../xv6-tutorial/questions/scheduling-and-synchronization.md#sched-07原-sync-09)。此处只保留兼容入口。

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
