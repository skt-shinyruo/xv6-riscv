# 文件系统与存储问题

本组问题关注 fd、file、inode、buffer 和磁盘块之间的多层生命周期，以及 redo log 和 VirtIO 中最容易误判的提交与完成边界。

## 问题

1. **FS-01** 已迁移到[文件系统问题 `FILESYS-06`](../xv6-tutorial/questions/filesystem.md#filesys-06-struct-dinode-struct-inode-与两个-inode-上界为什么不能合并)与[`FILESYS-09`](../xv6-tutorial/questions/filesystem.md#filesys-09-最后一个-pathname-消失后打开的文件为什么仍可读)。此处只保留兼容入口。
2. **FS-02** 已迁移到[文件系统问题 `FILESYS-09`](../xv6-tutorial/questions/filesystem.md#filesys-09-最后一个-pathname-消失后打开的文件为什么仍可读)。此处只保留兼容入口。
3. **FS-03** 已迁移到[文件系统问题 `FILESYS-11`](../xv6-tutorial/questions/filesystem.md#filesys-11-为什么-openo_create-返回--1-后-path-仍可能存在)。此处只保留兼容入口。
4. **FS-04** 已迁移到[持久化问题 `PERSIST-07`](../xv6-tutorial/questions/persistence.md#persist-07-begin_op-的-reservation-公式如何决定-sleep)。此处只保留兼容入口。
5. **FS-05** 已迁移到[持久化问题 `PERSIST-08`](../xv6-tutorial/questions/persistence.md#persist-08-end_op-返回为何不总等于本-operation-已-committed)与[`PERSIST-10`](../xv6-tutorial/questions/persistence.md#persist-10-真正的-commit-point-是-submit-还是-completion)。此处只保留兼容入口。
6. **FS-06** 已迁移到[持久化问题 `PERSIST-06`](../xv6-tutorial/questions/persistence.md#persist-06-ordinary-refwaitersleeplockpin-和-disk-owner-为什么不能合并)与[`PERSIST-09`](../xv6-tutorial/questions/persistence.md#persist-09-log_write-的-absorption-与-pinning-怎样协作)。此处只保留兼容入口。
7. **FS-07** 已迁移到[持久化问题 `PERSIST-04`](../xv6-tutorial/questions/persistence.md#persist-04-one-buffer-per-block-在-bget-的哪段原子成立)与[`PERSIST-05`](../xv6-tutorial/questions/persistence.md#persist-05-brelse-的顺序怎样限制-use-after-release-与-eviction)。此处只保留兼容入口。
8. **FS-08** 已迁移到[文件系统问题 `FILESYS-08`](../xv6-tutorial/questions/filesystem.md#filesys-08-readi-与-writei-在-partial-result-上有什么不同边界)。此处只保留兼容入口。
9. **FS-09** 已迁移到[恢复问题 `RECOVERY-09`](../xv6-tutorial/questions/recovery.md#recovery-09-日志恢复后为什么还要执行-ireclaim)。此处只保留兼容入口。
10. **FS-10** 已迁移到[设备中断与 VirtIO 队列问题 `DEVICE-10`](../xv6-tutorial/questions/device-io.md#device-10-为什么-8-个-descriptor-只能支持两笔三段请求在途)。此处只保留兼容入口。

## 源码入口

- [`kernel/sysfile.c`](../../kernel/sysfile.c)
- [`kernel/file.c`](../../kernel/file.c)
- [`kernel/fs.c`](../../kernel/fs.c)
- [`kernel/bio.c`](../../kernel/bio.c)
- [`kernel/log.c`](../../kernel/log.c)
- [`kernel/virtio_disk.c`](../../kernel/virtio_disk.c)
- [`kernel/pipe.c`](../../kernel/pipe.c)

## 配套文档

- [文件系统](../xv6-riscv/kernel/filesystem.md)
- [文件与管道](../xv6-riscv/kernel/files-and-pipes.md)
- [存储栈](../xv6-riscv/kernel/storage-stack.md)
- [一次文件系统事务](../xv6-riscv/flows/filesystem-transaction.md)
- [`unlink` 到崩溃后 orphan 回收](../xv6-riscv/flows/unlink-crash-orphan-recovery.md)
- [文件系统一致性](../xv6-riscv/filesystem/filesystem-consistency.md)
