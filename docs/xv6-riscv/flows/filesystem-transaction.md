# 一次文件系统事务：预留、提交与崩溃恢复

本文沿 `kernel/log.c` 的物理 redo log 追踪一个 `begin_op()` 到配对 `end_op()` 的日志操作区间：`begin_op()` 预留容量，inode/bitmap/data 修改通过 `log_write()` 登记，最后一个 outstanding 区间的 `end_op()` 触发 `commit()`，启动时再根据磁盘 header 恢复。并说明 `user/logstress.c` 与宿主 `test-xv6.py` 如何验证该协议。

下层 buffer/VirtIO 见[存储栈](../kernel/storage-stack.md)，inode 更新见[文件系统](../kernel/filesystem.md)，write 分片见[一次文件读写](file-read-write.md)。

## 1. 日志解决什么问题

一个高层操作可能更新多个 home block。例如创建文件并写首块可能涉及：

```text
parent directory data
parent/new inode block
allocation bitmap
new data block
possibly indirect block
```

若直接依次写 home locations，掉电可能只留下其中一部分：目录项指向未初始化 inode、inode 引用未标记块、bitmap 泄漏块等。redo log 先把所有新版本和目标块号持久化，再用一个 header 写作为提交点，使恢复能判断“整组重做”或“整组忽略”。

## 2. 物理日志格式

superblock 指定 `log.start` 与日志块数。磁盘布局：

```text
log.start + 0       header block
log.start + 1       copy of first modified home block
log.start + 2       copy of second modified home block
...
```

header 的 `struct logheader`：

```c
int n;
int block[LOGBLOCKS];
```

`n == 0` 表示磁盘没有已提交待安装事务；`n > 0` 时 `block[i]` 是第 i 个 log data block 最终要覆盖的 home block number。日志保存完整 1024 字节物理块，不理解 inode/目录/bitmap 内部格式。

当前 `LOGBLOCKS = MAXOPBLOCKS * 3 = 30`，磁盘日志实际还需一个 header block。`initlog()` 验证 header 结构严格小于 `BSIZE`。

## 3. 内存日志状态

全局 `struct log` 保存：

| 字段 | 含义 |
|---|---|
| `lock` | 保护以下内存协议字段 |
| `start`, `dev` | 磁盘日志位置/设备 |
| `outstanding` | 已 `begin_op` 但尚未配对 `end_op` 的日志操作区间数 |
| `committing` | commit 正在无锁执行磁盘 I/O，新操作必须等待 |
| `lh` | 当前 group transaction 的 unique home block 列表 |

多个日志操作区间可同时 outstanding，它们的 block 更新合并到同一 group transaction。只有 outstanding 降到 0 才提交，所以不会把一个仍在执行、只完成一半的受支持区间写入 committed header。区间与系统调用不是一一对应：大 `write()` 有多个区间，close/exit/exec/reclaim 等内核路径也会建立区间，而 pipe、设备文件及许多只读调用不建立区间。

## 4. 哪些路径必须建立事务

典型边界：

- `open(O_CREATE/O_TRUNC)`、mkdir、mknod、link、unlink、chdir；
- `kexec()` 的路径/inode读取，因为 name traversal 的 `iput` 理论上可触发释放；
- 普通 inode write 的每个 chunk；
- `fileclose()`/exit 释放最后 inode 引用；
- 启动 `ireclaim()` 回收每个 orphan inode。

纯普通文件 read 不建立事务，因为预期的 `[0,size)` 不会分配块。这个选择依赖文件没有洞：`readi()` 使用会分配的 `bmap()`，inode 若在 size 内出现 0 块地址，普通 `fileread()` 会走到 `balloc()`。没有其他 outstanding 区间时，第一次 `log_write()` 会因全局计数为 0 而 panic；若恰有别的区间，`log_write()` 不检查线程归属，该读可能无预留地把分配塞进对方 group；磁盘满时则以短读退出。`readi()` 又不负责 `iupdate()`，新 inode 地址可能不持久化并泄漏已登记块。正常 regular file 写入是 dense 的，但 `mkfs` 的 root size padding 在原目录大小恰好块对齐时也能留下一个尾洞，所以这不只是任意损坏镜像的假设边界。当前代码不把这些情况当作可恢复 I/O 错误或稀疏文件。任何正常情况下可能调用 `log_write()` 或让 `iput()` 进入 `itrunc()` 的调用链都必须在 transaction 内。

`kexec()` 展示了相反但同样危险的情况：它已经拥有自己的区间，损坏 executable 中的 hole 因此可以合法越过 `log_write()` 的全局 outstanding 检查并加入当前 group。事务存在只解决“能否登记”，不保证这次读取的分配有回滚或符合 10-block 上界；跨很多 ELF segment hole 时仍可超额并 panic，直接 inode 地址也没有 `readi()` 的 `iupdate()` 保证。正常 exec 依赖 dense inode 才是零日志项读取。

`log_write()` 会检查 `log.outstanding >= 1`，违反时 panic `log_write outside of trans`，把漏掉边界暴露为内核错误。

## 5. `begin_op()` 的容量预留

`begin_op()` 在 log spinlock 下循环检查：

```text
if committing:
  sleep(&log)
else if lh.n + (outstanding + 1) * MAXOPBLOCKS > LOGBLOCKS:
  sleep(&log)
else:
  outstanding++
  return
```

不是只看当前 `lh.n`，而是假定每个已进入和即将进入的操作仍可能再写 `MAXOPBLOCKS` 个 unique blocks。这样操作开始后不会因日志容量不足被迫在持 inode 锁和已修改状态时等待 commit。

当前容量 30、每项预留 10，因此在 `lh.n == 0` 时最多允许三个最坏操作并发进入。实际被 log absorption 合并的块会减少占用，但预留仍按最坏上界保守计算。

睡眠 channel 是 `&log`，`sleep(&log, &log.lock)` 原子释放条件锁并在唤醒后重新取得，避免错过 end/commit 状态变化。

## 6. 操作先修改 buffer cache

文件系统读取 home block 的唯一缓存 buffer，持 buffer sleeplock 修改 `bp->data`，然后调用：

```c
log_write(bp);
brelse(bp);
```

此时通常没有立即写 home block，也没有立即把数据写入 on-disk log。最新版本暂存在 buffer cache；`log_write` 只把 home block number 记入内存 header并 pin buffer。

这一延迟使同一事务对同一块的多次修改能合并，commit 时只把最终缓存版本写一份日志。因此“先完整修改，再 `log_write`”是调用协议的一部分；仅仅改过 cache buffer 并不自动把该块加入日志。

当前 `writei()` 的用户复制失败路径暴露了这个协议的边界。它先让 `copyin()` 直接写 `bp->data`，成功后才 `log_write(bp)`；跨用户页复制在后一页失败时，前一页对应字节已经改动，但失败分支只 `brelse()`。若 block 尚未登记，修改会无日志地留在 cache；若 block 已因新分配时的 `bzero()` 或同 group 早先更新而登记并 pin，commit 读取的是修改后的同一 buffer，失败复制的部分前缀仍会进入日志。日志层不会为调用者提供 buffer rollback。

## 7. `log_write()` 和 absorption

`log_write(b)` 在 log.lock 下扫描 `lh.block[0..n)`：

- 已有相同 `b->blockno`：不新增项，该块被 absorbed；
- 首次出现：写入 `lh.block[n]`，`bpin(b)` 增加 buffer refcnt，再 `n++`。

同一 home block 每个 group transaction 最多占一个 log slot。典型例子是多个写都修改同一个 bitmap 或 inode block；commit 复制的是其最终 buffer 内容。

pin 很关键：调用者随后 `brelse()` 后，该 buffer 仍不能作为 `refcnt == 0` victim 被重用于另一个块。否则内存 header 记录 block A，但 commit 时按 A 重新 bread 可能拿不到尚未写盘的最新数据。

函数先检查 `lh.n >= LOGBLOCKS` 并 panic `too big a transaction`，再做 absorption 扫描；正确 reservation 应保证永远不触及边界。

## 8. `end_op()` 的 group barrier

结束时在 log.lock 下 `outstanding--`：

- 若仍大于 0：wakeup(&log)，因为最坏预留减少，等待进入者可能已有空间；当前调用返回，但它的更新尚未提交。
- 若等于 0：设 `committing = 1`，由当前线程负责 commit。

实际 `commit()` 在释放 spinlock 后执行，因为 bread/bwrite/等待磁盘会睡眠。`committing` 阻止新的 begin_op 进入，保证提交期间 `lh` 和被 pin buffers 的集合不再变化。

commit 返回后，线程重新取得锁，清 committing，唤醒所有等待 begin_op 的进程。

因此一个日志操作区间的 `end_op()` 可能快速返回，也可能负责同步写完整个 group 的日志和 home blocks。

## 9. `commit()` 四阶段

当 `lh.n > 0`：

```text
1. write_log()
   cache home block versions -> log data blocks

2. write_head()
   persist n and home block-number list
   ===== true commit point =====

3. install_trans(0)
   log data blocks -> home locations
   bunpin each home buffer

4. lh.n = 0; write_head()
   erase committed transaction marker
```

每个 `bwrite()` 通过 VirtIO 同步等待请求完成后才返回。协议依赖这种完成顺序：header 不能在所有 log data blocks durable 前写出，clear header 不能在所有 home writes 完成前写出。

若没有 logged blocks，commit 什么也不写。

## 10. 阶段 1：写日志数据

`write_log()` 对每个 header 项：

```text
to   = bread(dev, log.start + i + 1)
from = bread(dev, home block number)
memmove(to->data, from->data, BSIZE)
bwrite(to)
release both
```

home buffer 仍是最新 cache version且被 pin。写 log data block 不改变 home location。此阶段磁盘 header 仍是 0，所以即使只写出部分 log data，重启也会忽略它们。

## 11. 阶段 2：header 是唯一提交点

`write_head()` 将内存 `lh.n` 和 block list 复制到 log header buffer，再同步 `bwrite()`。

在该写完成前，事务从恢复角度不存在；完成后，即使尚未安装任何 home block，恢复也拥有完整目标列表和先前已写完的全部 log data。原子性判定只依赖这一个 header block，而不是猜测各 home block状态。

这里假设一个块写不会留下恢复代码无法识别的 torn header，并假设 VirtIO 完成提供所需持久顺序；xv6 教学模型不实现校验和、序号、flush/FUA 或真实硬盘缓存断电协议。

## 12. 阶段 3：安装到 home locations

`install_trans(0)` 对每项读取 log copy 和 home buffer，整块复制后 `bwrite(home)`。写完调用 `bunpin(home)`，释放 `log_write` 增加的 ref。

安装可重复：把同样完整块再次覆盖到同一目标结果不变，这是 redo 幂等性的基础。某些 home blocks 可能在崩溃前已写，另一些未写，恢复无需判断，全部重做即可。

commit 期间 home buffer 仍保持其 identity；安装写的就是 cache 中事务最终版本对应的 log copy。

## 13. 阶段 4：清 header

全部 home block 同步写完后，内存 `lh.n=0` 并再次 `write_head()`。磁盘 header 清零表示无需恢复，log data blocks 内容可以留存，下一事务会覆盖。

只有 clear header完成后，被占用的日志槽才从磁盘协议视角完全空闲。此时 commit 返回并允许新 begin_op。

## 14. 崩溃窗口分析

| 崩溃时刻 | 磁盘 header | 重启行为 | 最终结果 |
|---|---:|---|---|
| 修改仅在 cache | 0 | 忽略 | 事务不生效 |
| 写了部分/全部 log data，尚未 header | 0 | 忽略 | 事务不生效 |
| header 已写，尚未 install | n>0 | 全部 redo | 事务完整生效 |
| install 到一部分 home | n>0 | 全部 redo | 事务完整生效 |
| home 全写，尚未 clear | n>0 | 全部 redo | 幂等，完整生效 |
| clear header 已写 | 0 | 无恢复 | home 已完整生效 |

因此不会恢复出只包含 group transaction 一部分 home 更新的已提交状态。

## 15. 启动恢复

`fsinit()` 读取并验证 superblock，然后 `initlog(dev, &sb)`：

```text
init log.lock/start/dev
read_head()                    disk header -> memory lh
install_trans(recovering=1)   redo every listed block
lh.n = 0
write_head()                  clear disk header
```

恢复模式每安装一项打印：

```text
recovering tail I dst BLOCK
```

恢复时没有对应 pinned home buffers，因为这是全新启动，所以 `install_trans(1)` 不调用 bunpin。

启动代码完全信任 header。若磁盘 `n < 0`，`read_head()` 的复制循环和 `install_trans()` 的安装循环都执行零次，随后 recovery 把 header 清零；它不会走数组负下标。若 `n > LOGBLOCKS`，`read_head()` 会立即越过源、目标两边声明的 30 项数组并从目标端破坏内核内存；只有当 `n` 继续大到超出一个磁盘块能容纳的整数数目时，源读取才越过整个 1024 字节 buffer。即使 `1 <= n <= LOGBLOCKS`，未经范围检查的目标 block 也可能造成任意位置覆盖，或让 log block 与 home block 是同一 buffer 而递归等待 sleeplock。三者都说明这是可信镜像假设，不能统称为一个相同的“n 越界”执行路径。

## 16. recovery 必须先于 orphan reclaim

本分支 `fsinit()` 在 `initlog()` 之后调用 `ireclaim()`。日志可能包含刚提交的 unlink/nlink/bitmap/dinode 更新；只有先 redo，磁盘 inode 才反映最后提交状态。

随后 `ireclaim` 扫描 `type != 0 && nlink == 0` 的 dinode，每个用独立新 transaction 截断并释放。这修复“unlink 后仍打开/cwd 持有时崩溃”留下的资源泄漏。

交换顺序可能按旧 home blocks 错过 orphan 或回收本不该回收的 inode。

## 17. 系统调用原子性与 group commit

日志一次可能包含多个并发日志操作区间。因为只有 `outstanding == 0` 才写 header，所以 group 中每个单次 `begin_op()`/`end_op()` 区间都已结束自己的 cache 修改，磁盘上的整个 group 一起提交或一起丢弃。对遵守日志协议、最坏块数不超限的单个区间，它的已登记更新因此作为 group 的子集全有或全无；更准确的说法不是“每个系统调用天然原子”，而是“每个受支持的日志操作区间包含在一次 group all-or-none 中”。

这不提供系统调用间隔离：并发可见性由 inode、buffer 和目录锁决定，一个区间可能在提交前看到另一个已完成但尚未持久化的 cache 更新。若它结束时仍有其他 outstanding 区间，`end_op()` 会直接返回，所以用户系统调用也可能先返回、随后崩溃时跟随尚未提交的整个 group 一起丢失。

all-or-none 也不表示返回 -1 的高层调用必然没有变化。日志只提交调用者最终登记的 buffer 版本，不理解成功返回值或自动回滚：除了 `writei()` 的失败分配，`open(path, O_CREATE)` 还可能先成功链接新空文件，再因 file/fd 表耗尽返回 -1；该创建仍属于本次 group 的已登记结果。

任意用户 syscall 也未必只含一个日志操作区间。`filewrite()` 主动把大 `write()` 切成最多 3072 字节的多次 begin/end；没有其他 outstanding 区间时，每片的 `end_op()` 会提交该片，有并发时，前一片可在尚未提交时返回，后续片又加入同一 group。因此分片是容量和协议边界，却不是彼此独立的 commit；崩溃可以在 group 边界留下写入前缀，整个大 `write()` 没有全有或全无保证。磁盘满或用户复制失败还可使单片内部只完成前缀，且 `writei()` 的分配和上述部分 buffer 修改不会回滚。

## 18. 容量为什么是正确性的条件

若一个已开始的区间实际触及超过 `MAXOPBLOCKS` 个新增 unique blocks，预留公式会低估，可能导致 `lh.n` 达到 `LOGBLOCKS` 并 panic。区间不能中途 commit，因为其 cache 更新尚非完整高层状态。`create/mkdir/link/unlink/itrunc/write` 的逐路径上界、并发 admission 推导及 `NBUF` 需求见[文件系统与日志资源上界](../kernel/resource-bounds.md)。

因此调用者必须：

- 把大 write 分片；
- 让 inode truncate/目录操作的参数上界与日志尺寸相容；
- 修改文件系统算法时重新核算 bitmap、data、inode、indirect和目录块最坏集合。

增加 `MAXOPBLOCKS` 还会改变 `LOGBLOCKS`、`NBUF` 和镜像布局，必须重建 `fs.img`。

## 19. buffer cache 与日志的所有权

每个 logged home buffer同时可能有：

- 调用者 `bread` 的临时 ref；
- `log_write` 首次登记的 pin ref；
- commit 中 `bread` 的临时 ref。

调用者 `brelse` 只移除第一项。pin 持续到 normal install 后 `bunpin`；因此 LRU 不能重用 dirty identity。log data buffers不需要长期 pin，它们在 commit 的同步 bwrite 完成后即可释放。

log absorption 依赖 buffer cache 对 `(dev, blockno)` 的唯一性：所有修改都汇聚到同一内存块，commit 读取到最终值。

## 20. `user/logstress.c` 的压力模型

`user/logstress.c` 对每个命令行文件 fork 一个进程。子进程打开自己的文件，设定：

```c
enum { N = 250, SZ = 2000 };
```

并循环执行核心调用 `write(fd, buf, SZ)` 250 次。每次 2000 小于 `filewrite()` 的 3072 chunk 上限，因此成功时恰好经过一次 `begin_op()`/`end_op()`；它可能与其他进程的操作合入同一 group，并不必然独占一次磁盘 commit。多个进程使 outstanding、容量预留、group absorption 和重复 commit 持续交错。

需要同时注意当前测试自身的两项边界：

- 全局 `buf` 由 `BUFSZ=500` 定义，却用 `memset(..., SZ=2000)` 并传 2000 字节给 write，超出 C 对象边界。它在当前布局可能运行，但属于未定义行为。
- 每个文件计划写 `250 * 2000 = 500000` 字节，而 `MAXFILE * BSIZE = 268 * 1024 = 274432` 字节；若进程未先被测试脚本强杀，第 138 次 2000 字节写会因越过上限返回 -1，循环不可能正常完成 250 次。

因此该程序实际依赖两秒强杀窗口来制造日志中断，不是一个能够自然跑完并验证最终内容的压力测试；异常时应同时排查用户缓冲区越界、文件上限和是否已错过目标 crash 窗口。

## 21. `test-xv6.py` 如何制造 log crash

宿主 `test-xv6.py` 的 `class QEMU` 启动 `make qemu` 并通过管道操纵 shell。`crash_log()`：

```text
build kernel + delete/rebuild fs.img
boot QEMU
send: logstress f0 f1 f2 f3 f4 f5
wait two seconds
find QEMU child process
SIGKILL it
```

这不是 xv6 内的正常 shutdown，而是直接丢失全部 RAM/buffer cache，保留已经到达 `fs.img` 的磁盘写，逼近掉电模型。

`recover_log()` 不重置镜像，重新启动并搜索行首 `recovering`。若检测到 recovery，再执行 `ls` 并要求出现 `f5`。

## 22. 为什么 log crash 最多重试五次

SIGKILL 由固定两秒延迟触发，没有插桩精确停在 write_head 后。一次运行可能：

- 尚未开始提交，header 为 0；
- 已提交并清 header；
- 恰好留下非零 committed header。

`test_log()` 最多进行五次 `crash_log -> recover_log`，首次观察到 recovery 且目录可用就成功。它验证恢复路径可工作，不保证每次都命中目标窗口，也不逐字节验证所有压力文件内容。

## 23. 完整 crash suite

`test_crash()` 顺序执行：

1. `test_log()`：redo header/install 恢复。
2. `test_forphan()`：打开后 unlink 文件，强杀，再期待 `ireclaim`。
3. `test_dorphan()`：cwd 持有已 unlink 目录，强杀，再期待 `ireclaim`。

这组测试反复删除/重建并修改 `fs.img`，不可与另一个 QEMU 实例并发使用同一镜像。成功依赖宿主能可靠找到并 SIGKILL QEMU 子进程；脚本实现限制详见[用户程序与测试](../user/programs-and-tests.md)。

## 24. 日志保证与不保证

保证：

- 非零 committed header 对应的所有 log data 已先写完。
- 恢复会把该列表所有块重做到 home locations。
- 一组 outstanding 区间只在全部 end 后 commit，所有按协议登记的 group blocks 全有或全无。
- 同一事务重复修改同块最终只记录一个最新版本。

不保证：

- 任意大小 write 的整体原子性；
- 失败的 `copyin()` 自动撤销已经写入 cache buffer 的前缀，或回滚 `bmap()` 已完成的分配；
- 返回 -1 的高层调用没有任何已登记副作用；
- 不同系统调用之间的 serializable 隔离；
- 用户数据内容具有 checksum；
- 存储硬件违反同步写/顺序假设时仍安全；
- 未提交 dirty cache 在崩溃后保留；
- 内存 inode ref 等非持久状态恢复，后者靠 `ireclaim` 补救特定 orphan 情况。

## 25. 常见错误模式

| 错误改动 | 后果 |
|---|---|
| 修改 buffer 后直接 `bwrite(home)` | 绕过 commit point，崩溃可见半操作 |
| 忘记 `begin_op` | `log_write outside of trans` panic |
| `end_op` 太早 | 未完成操作可能随 group commit 持久化 |
| 持 log spinlock 调 `commit` | 磁盘等待中睡眠持 spinlock，死锁/中断问题 |
| 不 pin logged buffer | dirty buffer可能被复用，日志写入错误版本 |
| header 早于 log data | 恢复会把不完整/旧 log data 当已提交 |
| clear header 早于 home install | 崩溃后无 header 可恢复未安装块 |
| recovery 后不清 header | 每次启动重复恢复；虽重做通常幂等，但状态永不空闲 |
| `ireclaim` 先于 log recovery | 按旧 nlink/type 扫描，可能漏回收或误判断 |
| 认为 `copyin` 失败前未改 buffer | 跨页失败可留下部分 cache 修改；已登记/pin 的块还可能把它提交 |

## 26. 调试事务时间线

建议断点：

```gdb
b begin_op
b log_write
b end_op
b write_log
b write_head
b install_trans
b commit
b recover_from_log
b ireclaim
```

每次记录：

```text
log.outstanding
log.committing
log.lh.n
log.lh.block[0..n)
buffer blockno/refcnt/disk
on-disk header n/list
```

若 begin_op 长期睡眠，检查 outstanding 区间是否漏 `end_op`、某线程是否持 inode 锁等待日志空间、commit 是否等待 VirtIO 中断。若恢复后结构损坏，核对 write_log 与 write_head 完成顺序、header block list 和 home block identity。

## 27. 验证命令

```sh
./test-xv6.py log
./test-xv6.py crash
```

普通非破坏性逻辑可通过完整 usertests 的 `bigwrite`、`manywrites`、`diskfull` 等提高覆盖。但上述宿主 crash 命令会重建并改变 `fs.img`；执行前应确认镜像状态可丢弃。

## 28. 核心不变量

1. `outstanding` 中每个日志操作区间都预留最多 `MAXOPBLOCKS`，实际 unique blocks 不得超过假设。
2. commit 期间没有新 operation 进入，也没有旧 operation继续修改 logged buffers。
3. 首次记录的 home buffer保持 pin 到 normal install 完成。
4. 所有 log data blocks 必须先于非零 header durable。
5. 所有 home installs 必须先于清零 header durable。
6. 非零 header 所列每个 log data block都可安全重复覆盖目标 home block。
7. 启动先 recovery，再进行依赖最终 dinode 状态的 orphan reclaim。

## 29. 核心结论

当前日志的真正提交对象不是单个 inode、单个系统调用或单个 write 分片，而是“outstanding 归零时积累的一组 unique home blocks”。`begin_op` 用最坏预留保证单次受支持操作能完成，`log_write` 用 absorption/pin 保存已登记 buffer 的最终版本，header 写把整组变成已提交，install/clear 将它稳定搬回 home。对所有遵守“完整修改后登记”、容量不超限的更新，崩溃窗口只能收敛到整个 group 提交前或提交后的状态；损坏文件洞和 `writei()` 的部分 `copyin` 失败属于当前实现未防御、也不由日志自动回滚的边界。
