# 文件系统与存储问题

本组问题关注 fd、file、inode、buffer 和磁盘块之间的多层生命周期，以及 redo log 和 VirtIO 中最容易误判的提交与完成边界。

## 问题

1. **FS-01** fd、`struct file.ref`、`inode.ref` 和磁盘 `nlink` 分别表示什么？为什么其中任何两个都不能合并？
2. **FS-02** 文件已经 `unlink`，但仍被进程打开时，为什么还能继续读写？最后由谁释放数据块？
3. **FS-03** `open(O_CREATE)` 创建目录项成功后，如果 file 表或 fd 表耗尽，为什么可能返回 `-1` 但文件仍然存在？
4. **FS-04** 为什么文件系统操作通常必须先 `begin_op()`，再获取 inode 锁？反过来可能形成什么死锁环？
5. **FS-05** `end_op()` 返回是否意味着数据已经持久化？redo log 真正的提交点是哪一步？
6. **FS-06** `log_write()` 为什么不立即写磁盘？pin buffer 和持有 buffer sleeplock 有什么本质区别？
7. **FS-07** 为什么 buffer cache 必须保证每个 `(dev, blockno)` 只有一个 buffer？`brelse()` 后继续使用指针会发生什么？
8. **FS-08** `filewrite()` 为什么可能已经写入并提交一部分数据，最终却仍返回 `-1`？
9. **FS-09** 日志恢复后为什么还要执行 `ireclaim()`？只重放 redo log 为什么不能回收崩溃前的 orphan inode？
10. **FS-10** VirtIO 有 8 个 descriptor，每个请求使用 3 个，为什么最多只有两笔请求在途？完成中断为什么不直接释放 descriptor？

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
