# 一次文件读写：用户缓冲区到磁盘块

本文沿普通文件的 `read`/`write` 追踪用户 stub、fd、全局 file、inode、buffer cache、redo log 和 VirtIO，并对比设备与 pipe 分支。各层细节见[系统调用](../kernel/system-calls.md)、[文件与管道](../kernel/files-and-pipes.md)、[文件系统](../kernel/filesystem.md)和[存储栈](../kernel/storage-stack.md)。

## 1. 三种 I/O 路径

同一个用户 API 经过 fd 对应的 `struct file.type` 分派：

```text
read/write(fd, user_va, n)
  -> sys_read/sys_write
  -> fileread/filewrite
       |- FD_INODE  -> ilock -> readi/writei -> bio/log/virtio
       |- FD_DEVICE -> devsw[major].read/write
       `- FD_PIPE   -> piperead/pipewrite
```

只有 `FD_INODE` 使用 file offset；它的写路径进入磁盘日志协议，读路径不调用 `begin_op()`。设备解释字节流的方式由驱动决定；pipe 使用内存环形缓冲和睡眠/唤醒。

## 2. 用户 ABI 到内核处理函数

用户调用：

```c
read(fd, buf, n);
write(fd, buf, n);
```

RISC-V ABI 已把 fd、地址、长度放在 a0/a1/a2，stub 设置 a7 并 ecall。`sys_read()`/`sys_write()`：

1. `argaddr(1)` 只提取用户虚拟地址，不直接解引用。
2. `argint(2)` 取得 32 位有符号长度。
3. `argfd(0)` 验证 `0 <= fd < NOFILE` 且 `ofile[fd]` 非空，返回共享 file 指针。
4. 调用 `fileread(f, va, n)` 或 `filewrite(f, va, n)`。

fd lookup 不增加 file ref，因为当前进程在系统调用期间不会并发 close 自己的槽。内核直到 copy helper 才验证 buffer 跨页范围。

## 3. fd、file 和 inode 三层身份

```text
process ofile[fd]
       -> global struct file
             type/readable/writable/off/ref
             -> struct inode
                   dev/inum/nlink/size/addrs/lock
```

`dup()` 和 `fork()` 让多个 fd 槽指向同一个 `struct file`，所以共享 `off`。同一路径分别 open 两次则得到不同 file 对象和独立 offset，但都指向同一 inode。

硬链接改变的是多个目录名指向同一 inode；每次 open 仍新建 file 对象。unlink 名字不会使已打开 file 失效，因为 inode `ref` 仍存在。

## 4. 读权限和类型分派

`fileread()` 首先要求 `f->readable != 0`。随后：

- pipe：`piperead(f->pipe, user_va, n)`；
- device：验证 major 和函数指针，调用 `devsw[major].read(1, va, n)`；
- inode：锁 inode，调用 `readi(ip, user_dst=1, va, f->off, n)`，成功正数时推进 offset，再解锁。

`user_dst=1` 告诉下层目标是用户 VA，必须用 `copyout`；内核自己的目录/ELF读取传 0，直接复制到内核地址。

## 5. inode 锁同时保护内容和共享 offset

普通文件路径在 `ilock(ip)` 后读取和更新 `f->off`：

```c
if ((r = readi(..., f->off, n)) > 0)
  f->off += r;
```

虽然 `off` 属于 file 而不是 inode，使用 inode sleeplock 仍能序列化共享该 file 的 fork/dup 读者；同 inode 的所有打开对象也被更强地串行化。锁覆盖磁盘等待和用户复制，因此一次 read 的内容与 offset 前进作为一个临界区观察。

没有独立 seek 系统调用。open 普通文件把 off 设为 0，读写只能顺序推进；非截断重开也从 0 开始。

## 6. `readi()` 的边界计算

调用者持 inode 锁。`readi()`：

```text
if off > size or off+n wraps: return 0
if off+n > size: n = size-off
for each covered block:
  addr = bmap(ip, off/BSIZE)
  bread(dev, addr)
  m = min(remaining, BSIZE - off%BSIZE)
  either_copyout(user_dst, dst, block+offset, m)
  brelse
return total
```

正好 EOF 返回 0，跨 EOF 截短。`bmap()` 没有“只查询”模式：映射缺失时它会尝试 `balloc()`，而分配路径需要 `log_write()`。普通 read 没有 `begin_op()`，所以正确性依赖被读 inode 的 `[0,size)` 已全部映射；若该范围出现 hole，读路径可能因 `log_write outside of trans` panic，或在磁盘已满时得到 0。正常 `writei()` 和 `mkfs:iappend()` 建立的 regular file 满足 dense 前提，但 `mkfs` 对 root directory 的特殊 size padding 在原大小恰好块对齐时也会制造尾洞，并非只有任意外部损坏镜像才可能违反它。不能把这里的 `bmap()` 调用解释为不会修改状态的查询 API。

同一个 `readi()` 还被 `kexec()` 用来读 ELF，但那条调用链已经进入 `begin_op()`。损坏 executable 的 hole 因而不会在第一次 `log_write()` 处按“事务外 read”失败，而会真正分配 bitmap/零块/间接项并加入 exec 的日志组；loader 后续失败不回滚这些文件系统副作用，直接地址又可能因没有 `iupdate()` 而泄漏。一次装载跨越的 hole 数也没有被 write 分片限制，可能超过 10-block 预留并在日志或 buffer cache 边界 panic。普通 dense 文件仍只走查询路径，这个差异来自调用上下文和损坏 inode，而不是两套 `readi()` 实现。

## 7. buffer cache 和磁盘读取

`bread(dev, blockno)` 先由 `bget()` 在 buffer cache 中寻找唯一 `(dev, blockno)` buffer：

- 已缓存：增加 ref，获取 buffer sleeplock；若 valid 可直接使用。
- 未缓存：选择 `refcnt == 0` 且未 pin 的槽，设置 identity，必要时发 VirtIO read，等待中断完成。

读取者得到已锁 buffer，数据在 `bp->data[1024]`。`brelse()` 解 sleeplock并减 ref，将其移到 MRU 位置。inode 锁防文件级变化，buffer 锁防同一块的并发数据竞态。

VirtIO 一次请求使用 header/data/status 三描述符链。设备先写 status 并产生中断；`virtio_disk_intr()` 验证 status、把 `b->disk` 清零并 `wakeup(b)`。提交线程从 `sleep()` 返回后才清除 `disk.info` 并调用 `free_chain()` 释放三个描述符，中断处理器本身不释放它们。

## 8. 从块复制到用户页

`either_copyout(1, user_va, src, m)` 调用 `copyout()`。它按用户页拆分：

1. `walkaddr()` 找物理页。
2. 合法 lazy 地址未映射时调用 `vmfault()` 分配零页。
3. 检查 PTE 可写，禁止把文件数据覆盖只读 text。
4. 以物理地址执行 memmove。

因此 read 可以成为用户 lazy buffer 的首次触页入口。若后续页无效，`readi()` 释放当前 buffer并把总结果设为 -1；已经复制到前面用户页的数据不会撤销，但 file offset 因返回不大于 0 而不推进。这种失败后用户内存与 offset 可能不一致，是简单错误语义的结果。

## 9. 普通文件读的返回值

| 条件 | 返回 | offset |
|---|---:|---:|
| 读取 n 字节成功 | n | `+n` |
| 到 EOF 前只剩 k 字节 | k | `+k` |
| 已在 EOF | 0 | 不变 |
| 用户复制失败 | -1 | 不变，即使前缀已复制 |
| fd 不可读或设备分派无效 | -1 | 不变 |
| `struct file.type` 为未知值 | kernel panic | 不适用 |

负长度没有在统一入口被拒绝。inode read 会把 `int` 隐式转成 `uint` 传给 `readi()`：根据当前 `off`，加法可能回绕并返回 0，也可能先被钳制到 EOF 而读取剩余内容；pipe 和设备又有各自结果。API 调用者必须传非负长度，不能把任一后端的偶然行为当成稳定 ABI。

## 10. 写权限和分派

`filewrite()` 首先要求 writable。pipe 和 device 直接调用各自实现；普通 inode 不能让任意大的 `n` 占用一个日志预留范围，因此进入 chunk loop。

每个 chunk：

```text
begin_op()
ilock(inode)
writei(user_src=1, addr+i, f->off, n1)
if r > 0: f->off += r
iunlock(inode)
end_op()
```

`begin_op()` 在取得 inode 锁前为最坏更新预留日志空间，返回时不继续持有 `log.lock`。`end_op()` 发现全局 outstanding 归零时会同步执行 group commit；否则只退出本轮范围，修改继续留在共享的内存日志组中。

## 11. 为什么每块数据可能需要两个日志块

写入一个新文件数据块可能修改：

- 数据 home block；
- bitmap block；
- inode block中的 size/addrs；
- 进入间接范围时的 indirect block；
- 非对齐/边界产生的额外块。

`filewrite()` 用保守公式：

```c
max = ((MAXOPBLOCKS - 1 - 1 - 2) / 2) * BSIZE;
```

当前 `MAXOPBLOCKS=10`、`BSIZE=1024`，得到 3072 字节。减项为 inode、indirect 和两块余量，除 2 估计每数据块还需 allocation 更新。这个公式表达的是设计时的保守估算，不是对任意镜像布局的独立定理；在当前单 bitmap 镜像、正常无 hole 文件和连续写前提下，逐块集合证明给出的真实上界是 7，因而 10-block reservation 足够。扩大到多 bitmap 布局、允许 sparse write 或改变分配算法时必须重新证明，详见[资源上界](../kernel/resource-bounds.md#10-一个-write-chunk-的严格推导)。

## 12. `writei()` 的边界

调用者持 inode 锁。函数拒绝：

- `off > ip->size`，不支持越过 EOF 制造 sparse hole；
- `off + n` 溢出；
- 结束位置超过 `MAXFILE * BSIZE`。

逐块执行：

```text
addr = bmap(ip, logical block)       allocate if absent
bp = bread(dev, addr)
m = min(remaining, bytes to block end)
either_copyin(user_src, bp slice, user va, m)
log_write(bp)
brelse(bp)
```

最后按实际前进的 off 扩大 size，并无条件 `iupdate()`；即使 size 未变化，bmap 也可能刚写入 inode addrs。

## 13. 块分配到日志

新逻辑块的路径：

```text
bmap
  -> balloc scans bitmap
       set allocation bit; log_write(bitmap buffer)
       bzero(new block); log_write(data buffer)
  -> direct: update ip->addrs in memory
  -> indirect: log_write(indirect buffer)

writei
  -> modify data; log_write(data buffer)   # absorbed if already listed
  -> iupdate; log_write(inode buffer)
```

同一块在当前内存日志组中多次 `log_write()` 只占一个 header 项，这叫 log absorption。buffer 被 pin，不能在 commit 把最新缓存内容复制到日志前被回收为别的磁盘块。

## 14. commit 到 VirtIO

最后一个 outstanding operation 的 `end_op()` 触发：

```text
write_log: cache home buffers -> on-disk log data blocks
write_head: persist block list/count                     COMMIT POINT
install_trans: log data blocks -> home blocks
write_head(n=0): clear log
```

每个 `bwrite()` 最终经 VirtIO 三描述符请求并等待完成中断。commit point 前崩溃忽略未发布日志；之后崩溃由启动 recovery 重做到 home blocks。完整协议见[一次文件系统事务](filesystem-transaction.md)。

## 15. 大 write 拆成多个日志操作范围

若用户请求 10000 字节，当前最大 chunk 3072，调用范围一定按如下大小拆分：

```text
3072 begin/end -> 3072 begin/end -> 3072 begin/end -> 784 begin/end
```

锁在每个 chunk 后释放，其他进程可在 chunk 间读写同一 inode。若系统中没有其他 outstanding 文件系统操作，每个 `end_op()` 都看到计数归零，于是这一序列也恰好对应四次同步 commit。存在并发时，某轮 `end_op()` 可能不提交，下一轮以及其他进程的修改可以进入同一个 group transaction；上面的四个范围不保证对应四个磁盘 header 提交。

整个 10000 字节调用仍不是一个日志原子单元。崩溃可能保留已经完成提交的前缀 group，并丢弃当前尚未发布 header 的 group；但若多个 chunk 恰好在同一 group 中，它们会随该 group 一起保留或一起丢失。

系统调用返回也不是持久化屏障。若另一个文件系统操作一直 outstanding，本次 write 的各轮 `end_op()` 都可能只减少计数，整个 `sys_write()` 返回后，相关 group header 仍尚未写盘；此时崩溃可以丢失已经向用户报告成功的缓存态修改。

对经 dup/fork 共享同一 file 的 writer，f->off 每 chunk 在 inode 锁内推进，两个调用可能以 chunk 粒度交错，而不是每个完整 write 调用连续。不同 open file 对象有独立 offset，也仍由 inode 锁串行实际修改。

## 16. 写失败、缓存副作用与部分持久化

`writei()` 在空间不足或用户 `copyin()` 失败时返回此前完整完成的字节数。较早成功循环的 buffers 已经 `log_write()`；当前 `end_op()` 会结束操作范围，但只有它令全局 outstanding 归零时才立即提交，否则这些修改留待 group commit。`filewrite()` 在 `r > 0` 时按这个已完成计数推进 offset。

若一次 `copyin()` 跨用户页，它可能先改写当前 `bp->data` 的前缀，再因后一页无效返回 `-1`。`writei()` 此时尚未执行本轮的 `log_write(bp)`，这段前缀不计入 `r`，offset 也不推进；但缓存内容已经部分变化。若该块此前已因 `bzero()` 或 log absorption 登记，后续 commit 仍可能包含 buffer 的最新前缀，否则该修改没有本轮日志项，只是暂时的内存可见副作用。

但 `filewrite()` 只有 `r == n1` 才把该 chunk 加到累计 `i`；任一短 chunk后跳出，最终：

```c
ret = (i == n ? n : -1);
```

所以用户可能得到 -1，同时文件已经写入部分数据、扩大 size、推进共享 offset。此前完整 chunks 已完成运行时修改，其中一些可能已经提交，另一些可能仍属于尚未提交的并发 group。这不是“错误则零效果”的接口，重试前必须重新检查文件内容；系统没有 seek，通常需 close/reopen 才从 offset 0 处理。

## 17. 覆盖写和文件增长

打开已有文件但不 `O_TRUNC` 时 offset 为 0，write 覆盖开头；超过旧 EOF 才增长。当前没有 append mode，shell `>>` 只是不传 `O_TRUNC`，仍从 0 开始覆盖，而非定位末尾。

`O_TRUNC` 在 `open()` 自己的 `begin_op()`/`end_op()` 范围内调用 `itrunc()`，释放所有块并令 `size=0`；源码甚至没有要求同时带写权限。后续第一次 write 失败不会逻辑回滚这次截断。没有其他 outstanding 操作时，`open()` 的 `end_op()` 会在返回前提交；存在并发 group 时，截断可能在内存中已完成但仍要等全局 outstanding 归零才写出提交 header。

## 18. pipe read/write 对比

pipe 没有 inode、offset 或日志。512 字节环形区由 `nread/nwrite` 表示逻辑累计读写量；实际字段是 `uint`，会回绕，源码依靠无符号等式和 `% PIPESIZE` 保持空、满与槽位语义：

- full：writer wake readers并睡在 `&nwrite`；
- empty 且 write end open：reader 睡在 `&nread`；
- 所有 write ends 关闭且 empty：read 返回 EOF 0；
- read ends 全关：write 返回 -1。

`copyin`/`copyout` 按一个字节执行，也可补合法 lazy 页。`piperead()` 若坏目标发生在首字节返回 -1，已有前缀则返回部分字节；`pipewrite()` 的坏源会 break 并返回已经写入的字节数，哪怕是 0。若读端关闭或 writer 观察到 killed，`pipewrite()` 返回 -1，即使此前已写入部分数据；reader 只在“空且写端仍开”的等待路径检查 killed，有缓存数据时仍可读取。kill 若落在检查标志后、`sleep()` 取得 `p->lock` 前，目标仍可能睡到对端状态变化或第二次 kill，不能把“有检查”理解为取消没有窗口。空且写端仍开的零长度 read 也会先进入等待循环。其错误语义与 inode file 不完全相同。

pipe 是字节流，不保留 write 边界；缓冲满而睡眠时会释放 `pipe.lock`，多个 writer 的长写可以交错。

pipe lock既保护数据/计数，也与 `sleep(chan, &lock)` 配合防丢失唤醒。close 最后一读/写端时唤醒对端，使其重新检查 EOF/破管道条件。

## 19. 设备 read/write 对比

设备 file 保存 major，`fileread/write` 验证范围与 `devsw[major]` 函数存在，再传：

```text
read(user_dst=1, user_va, n)
write(user_src=1, user_va, n)
```

console 读由输入环形缓冲、行编辑和睡眠控制；UART 输出在当前分支以一字节 `tx_busy` 加 THRE 中断同步推进，不是软件 TX ring。THRE 只表示发送保持寄存器可以接收下一字节，不表示前一字节已在串行线路上发送完毕。设备没有普通 file offset，具体短读/短写和 killed 行为由驱动实现。

## 20. 并发锁顺序

普通文件一次 chunk 的真实锁序可概括为：

```text
begin_op:
  acquire log.lock; 必要时 sleep(&log, &log.lock); 增加 outstanding; release
ilock(inode)                                      # inode sleeplock
  bread/bmap:
    短暂 acquire bcache.lock 选择唯一 buffer
    acquire buffer sleeplock
    若需要磁盘 I/O，acquire disk.vdisk_lock 并 sleep；等待时仍持 inode/buffer lock
  log_write（通常仍持 buffer lock）:
    acquire log.lock
    首次登记时 bpin 再短暂 acquire bcache.lock
    release log.lock
  brelse buffer
iunlock(inode)
end_op:
  acquire/release log.lock；若 outstanding 归零，释放 log.lock 后 commit
```

`begin_op()` 可能睡眠，但那时还未持 inode 锁。磁盘等待会通过 `sleep(..., &disk.vdisk_lock)` 原子释放 VirtIO 锁，不过调用链仍持 inode 和当前 buffer 的 sleeplock。代码不会持 `log.lock` 等待磁盘；commit 在释放 `log.lock` 且已经释放本次 inode 锁后运行。buffer cache 保证同块唯一缓存对象，inode lock 则保证文件 `size/addrs` 和 offset 操作一致。

## 21. 资源生命周期

read/write 本身不增加 file ref。系统调用期间进程 fd 持有它；fork/dup 增加 ref，close/exit 减少。最后 fileclose：

- pipe：关闭对应方向，必要时释放 pipe 页；
- inode/device：在事务中 `iput()`，可能回收 nlink=0 inode；
- 普通仍有名字 inode：只减内存 ref。

buffer 在每次 bread 后必须 brelse；被日志首次记录时额外 bpin，直到 install_trans 后 bunpin。

## 22. 关键不变量

1. fd 必须指向 ref>0 的全局 file；readable/writable 在分派前检查。
2. inode size、addrs、内容操作和共享 file offset 的更新都在 inode lock 内。
3. `[0,size)` 正常不含 block hole；普通 read 不应触发分配。
4. 所有准备持久化的数据、bitmap 和 inode 修改必须经 `log_write()`，且调用时至少有一个 outstanding 日志操作范围。
5. 单个 write chunk 的最坏 unique block 数不能超过 reservation。
6. 同一磁盘块最多一个 buffer cache 身份，logged buffer 在安装前保持 pin。
7. 用户地址只经 copyin/out 访问；copyout不得写只读 PTE。
8. 大 write 和失败 write 允许部分持久效果，调用者不能假设全有或全无。

## 23. 一次无并发的 4 KiB 新文件写示例

```text
sys_write(fd, buf, 4096)
  filewrite max=3072
    operation A (3072):
      allocate/write first 3 data blocks
      update bitmap + inode
      end_op -> outstanding=0 -> group commit A
    operation B (1024):
      allocate/write fourth block
      update bitmap + inode
      end_op -> outstanding=0 -> group commit B
  return 4096
```

这个图明确假设没有其他 outstanding 文件系统操作；有并发时 A、B 未必对应两个 commit。若第二轮用户复制失败，在该假设下第一轮 3072 字节已经提交；第二轮可能已经分配并清零某块、更新 inode block 指针但不扩大到失败前缀，最终用户看到 -1。日志保证每个已提交 group 内部结构一致，不保证整个调用级回滚。

## 24. 验证和断点

功能测试：

```sh
./test-xv6.py writetest
./test-xv6.py writebig
./test-xv6.py sharedfd
./test-xv6.py bigwrite
./test-xv6.py badwrite
```

GDB：

```gdb
b sys_read
b fileread
b readi
b sys_write
b filewrite
b writei
b log_write
b commit
b virtio_disk_rw
```

记录每层的 `(fd, f, ip, off, n, blockno, log.outstanding, log.lh)`，能定位错误发生在用户拷贝、offset、block mapping、日志还是设备完成。运行宿主测试会重建/修改 `fs.img`，不要与另一个 QEMU 共享镜像。

## 25. 核心结论

普通文件 I/O 的正确性来自三种不同粒度：inode 锁保证一次 chunk 的内存并发一致，redo log 保证一次已提交 group transaction 的崩溃一致，file offset 通过共享 file 对象表达 fork/dup 语义。它们都没有把一个任意大小 write 提升为调用级原子操作；理解分片、group commit 和部分失败，才能正确解释磁盘满、坏用户指针与多进程并发的结果。
