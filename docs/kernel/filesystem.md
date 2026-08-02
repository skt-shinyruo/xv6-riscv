# 文件系统：inode、目录、路径与命名操作

本文说明当前仓库 `kernel/fs.c`、`kernel/fs.h`、`kernel/file.c`、`kernel/file.h`、`kernel/stat.h` 和 `kernel/sysfile.c` 中的文件系统核心逻辑。重点是磁盘格式与内存对象之间的关系、锁和引用计数、目录与路径遍历、创建/链接/删除的事务协议，以及本分支启动时额外执行的 orphan inode 回收。

本文把 buffer cache、redo log 和 VirtIO 当作下层服务；其实现见[存储栈](storage-stack.md)。fd、全局 `struct file` 和管道见[文件与管道](files-and-pipes.md)，系统调用参数入口见[系统调用](system-calls.md)。

## 1. 分层和调用边界

文件相关用户请求大致经过：

```text
user system-call stub
  -> kernel/syscall.c
  -> kernel/sysfile.c           argument checks + namespace transaction boundaries
  -> kernel/file.c              open-file dispatch/offset + write/close boundaries
  -> kernel/fs.c                inode, directory, pathname, block mapping
  -> kernel/log.c               redo transaction
  -> kernel/bio.c               cached block
  -> kernel/virtio_disk.c       physical I/O
```

`kernel/fs.c` 自己再分为五层：原始块分配、inode、文件内容、目录、路径名。高层可以组合低层操作，但必须满足两个跨层前提：

1. 任何可能修改磁盘状态、以及任何可能通过 `iput()` 触发删除的路径，都必须位于 `begin_op()`/`end_op()` 事务内。
2. 读取或修改 inode 的缓存元数据与内容时必须持有该 inode 的 sleeplock；只持有一个 inode 指针引用并不等于已锁定。

## 2. 磁盘布局与 `kernel/fs.h`

`kernel/fs.h` 定义内核和宿主 `mkfs` 共享的磁盘 ABI：

```text
[ boot | superblock | log | dinode blocks | bitmap | data blocks ]
```

当前关键常量：

| 常量 | 值 | 含义 |
|---|---:|---|
| `BSIZE` | 1024 | 文件系统块大小 |
| `ROOTINO` | 1 | 根目录 inode 编号 |
| `FSMAGIC` | `0x10203040` | superblock 格式标识 |
| `NDIRECT` | 12 | inode 内直接块地址数 |
| `NINDIRECT` | 256 | 一个间接块可容纳的 32 位地址数 |
| `MAXFILE` | 268 | 单文件最多数据块数 |
| `DIRSIZ` | 14 | 目录项名字的固定字节数 |

`struct superblock` 保存总块数、数据块数、inode 数、日志大小以及 log/inode/bitmap 区域起点。当前内核只有一个全局 `sb`，注释明确承认理论上应每个磁盘设备各有一个 superblock；实现实际上只支持根磁盘这一套布局。

### 2.1 Superblock 和磁盘元数据的信任边界

`fsinit()` 从块 1 复制 `struct superblock` 后只检查 `sb.magic == FSMAGIC`。它不验证 `size/nblocks/ninodes/nlog` 之间的算术关系，也不验证 `logstart/inodestart/bmapstart` 是否递增、互不重叠、落在 `sb.size` 和 VirtIO 设备容量内。定位宏随后直接使用这些字段计算块号。因此 magic 正确但其他字段损坏的镜像可能触发越界 I/O、让不同元数据区域互相覆盖、造成 panic，甚至把错误数据安装到合法块；当前代码没有“拒绝挂载损坏镜像”的完整验证层。

日志恢复继续信任磁盘 log header：`read_head()` 没有先把 `lh.n` 限制到 `LOGBLOCKS`，也不检查每个 home block 是否位于文件系统范围或避开日志区。bitmap、dinode block 地址和目录项 inode 号同样只在各自使用路径接受有限的局部不变量检查。`mkfs` 生成的正常 `fs.img` 满足布局契约，但这不是面向恶意或任意损坏磁盘的安全边界。若要支持外部镜像，应在任何恢复或 block 访问前增加集中式、使用防溢出算术的 superblock/log header 校验，并为 bitmap、inode 和目录结构提供离线一致性检查。

定位宏将逻辑编号转为磁盘块：

```text
IPB             = BSIZE / sizeof(struct dinode)
IBLOCK(i, sb)   = i / IPB + sb.inodestart
BPB             = BSIZE * 8
BBLOCK(b, sb)   = b / BPB + sb.bmapstart
```

修改 `struct dinode`、`struct dirent` 或相关常量会改变磁盘格式，必须同步重建 `mkfs/mkfs` 和 `fs.img`。初始镜像生成过程见[构建、链接与文件系统镜像](../build/build-link-and-fs-image.md)。

## 3. 三种“文件类型”表示

`kernel/stat.h` 定义 inode 类型：

```text
T_DIR = 1       directory inode
T_FILE = 2      regular inode
T_DEVICE = 3    device inode
```

这与 `kernel/file.h` 中打开文件对象的 `FD_INODE`、`FD_DEVICE`、`FD_PIPE` 不是同一枚举：前者持久化在 inode，后者描述一次打开后的 I/O 分派对象。普通目录也以 `FD_INODE` 打开，但只有只读模式获准。

`struct stat` 是交给用户态的只读快照，字段包括设备号、inode 编号、类型、链接数和字节大小。`stati()` 在 inode 已锁时从内存 inode 填充它；它不暴露块地址、major/minor 或引用计数。

## 4. 磁盘 inode 与内存 inode

一个文件有两种表示。

`struct dinode` 持久化：

```text
type, major, minor, nlink, size, addrs[NDIRECT + 1]
```

`struct inode` 是缓存项，除上述副本外还有：

```text
dev + inum      stable cache identity
ref             number of in-memory pointer references
sleeplock       protects loaded fields/content operations
valid           whether disk fields have been loaded
```

`nlink` 与 `ref` 解决不同生命周期问题：

- `nlink` 是文件系统维护的持久链接计数。非目录 inode 上它等于指向 inode 的硬链接名字数；成功创建的非根目录以父目录中的名字为基础计数 1，每个子目录的 `..` 再为它增加一次，而自己的 `.` 不计数。根目录由 `mkfs` 特别初始化为 1。因此目录的 nlink 不是所有 dirent 的字面计数；归零表示 inode 已没有受计数的命名/目录结构链接。
- `ref` 统计内核当前持有的内存指针，例如打开文件、进程 cwd、路径遍历临时引用。
- 只有 `nlink == 0` 且最后一个 `ref` 被释放时，数据块和 dinode 才真正释放。

因此打开文件可以在 `unlink()` 后继续读写；目录项立即消失，但打开的 `struct file -> inode` 仍维持 `ref`。

## 5. inode cache 的锁分工

全局 `itable` 有 `NINODE` 个槽位：

```c
struct {
  struct spinlock lock;
  struct inode inode[NINODE];
} itable;
```

锁规则是：

| 字段/动作 | 保护锁 |
|---|---|
| cache 槽是否空闲、`ref`、`dev`、`inum` | `itable.lock` spinlock |
| `valid`、type、major/minor、nlink、size、addrs 和内容操作 | `ip->lock` sleeplock |

`iinit()` 初始化表锁和每个槽位的 sleeplock，但不读磁盘。spinlock 只保护短小的表扫描和计数变化；可能等待磁盘的加载/读写放在 sleeplock 下，允许持锁进程睡眠。

缓存不是按块内容的复制缓存；对同一 `(dev, inum)`，只允许存在一个 `ref > 0` 的内存 inode 项。这个唯一性使 sleeplock 能串行化所有进程对同一 inode 的元数据和内容访问。

## 6. `iget()`、`idup()` 和延迟加载

`iget(dev, inum)` 在 `itable.lock` 下扫描：

1. 若找到相同 `(dev, inum)` 且 `ref > 0` 的槽，递增 `ref` 后返回。
2. 同时记住第一个 `ref == 0` 的空槽。
3. 若没有命中，使用空槽写入 identity、设置 `ref = 1`、`valid = 0`。
4. 若 50 个槽全部被引用，直接 `panic("iget: no inodes")`，不是可恢复的 `ENFILE`。

它不获取 inode sleeplock，也不读取 dinode。这样路径遍历可以先持有稳定引用，再按需要短时间锁 inode，避免 cache 表锁跨磁盘 I/O。

`idup(ip)` 只在表锁下递增 `ref`，常用于复制 cwd 或为路径遍历取得独立所有权。调用者必须已经拥有一个有效引用。

## 7. `ilock()` 与 `iupdate()`

`ilock(ip)` 先验证非空且 `ref >= 1`，再获取 sleeplock。若 `valid == 0`，它通过 `IBLOCK` 读 dinode，将所有磁盘字段复制到内存并设 `valid = 1`；加载到 `type == 0` 说明调用者引用了未分配 inode，触发 panic。

返回后调用者同时拥有：

- 一个防止 cache 槽复用的引用；
- inode sleeplock，可稳定检查/修改元数据和内容。

`iunlock()` 通过 `holdingsleep()` 验证锁记录的 pid 是当前进程、且引用仍存在，然后释放；sleeplock 所有者按进程而不是按 CPU 标识，进程睡眠后可换 CPU 继续运行。

任何对 type、major/minor、nlink、size 或 addrs 的持久修改都必须在锁内调用 `iupdate()`。它把完整字段集合复制回相应 dinode buffer，并调用 `log_write()`；buffer 只是加入当前事务，不一定当场写入其 home block。

### 7.1 磁盘 inode 分配 `ialloc()`

`ialloc(dev, type)` 不从 inode cache 找“空槽”，而是在外层事务中直接扫描磁盘 dinode：

1. 从保留的 inode 0 之后开始，遍历 `inum = 1 .. sb.ninodes - 1`。
2. `bread(IBLOCK(inum, sb))` 锁住对应 buffer，检查该位置的 `dip->type`。
3. 遇到 `type == 0` 时清零整个 `struct dinode`，只设置请求的 type，再 `log_write()` 该 dinode block。
4. 释放 buffer，并用 `iget(dev, inum)` 返回有引用、未锁且尚未由 `ilock()` 加载字段的内存 inode。

buffer cache 对同一磁盘块的唯一性及 buffer sleeplock 让并发扫描者串行观察 type 更新，不会分配同一个 dinode。这里直接修改的是 dinode buffer，所以初始 type 不经过 `iupdate()`；调用者随后锁 inode，填写 major/minor、`nlink` 等字段并 `iupdate()`。全部 inode 已占用时打印 `ialloc: no inodes` 并返回 0，不会修改任何 dinode。

## 8. `iput()` 的最后引用删除协议

普通情况，`iput()` 在 `itable.lock` 下递减 `ref`。特殊情况为：

```text
ref == 1 && valid && nlink == 0
```

这意味着当前调用者是最后一个内存引用，且磁盘上没有名字。它会：

```text
hold itable.lock
  acquire inode sleeplock
  release itable.lock
  itrunc(ip)             free data and indirect blocks
  ip->type = 0
  iupdate(ip)            mark dinode free
  ip->valid = 0
  release inode sleeplock
reacquire itable.lock
  ref--                  slot becomes reusable
release itable.lock
```

通常不应持 spinlock 获取可能睡眠的锁，但这里 `ref == 1` 证明没有其他内核引用者能够持有该 inode sleeplock，所以 `acquiresleep()` 不会阻塞。这也是为什么检查必须与表锁下的引用计数原子结合。

释放数据会修改 bitmap、间接块和 dinode，因此所有 `iput()` 调用者都被要求处于文件系统事务内，即使多数调用实际不会触发删除。调用 `iput(ip)` 时还不能持有同一个 `ip->lock`：删除分支会再次 `acquiresleep()` 而自锁，普通分支先丢最后引用再 `iunlock()` 也会破坏生命周期检查。已锁 inode 必须使用 `iunlockput()`，即先解锁、再释放引用。

## 9. 启动：日志恢复必须早于 orphan 回收

`fsinit(dev)` 在首进程的 `forkret()` 中只执行一次：

```text
readsb(dev, &sb)
validate sb.magic == FSMAGIC
initlog(dev, &sb)         recover committed redo log if present
ireclaim(dev)             reclaim nlink==0 allocated dinodes
```

顺序不能交换。崩溃时磁盘可能仍有一个已提交但尚未安装到 home blocks 的日志；只有 `initlog()` 恢复后，dinode 的 `type`、`nlink` 和块映射才是最后提交事务的一致状态。`ireclaim()` 必须扫描这个恢复后的状态。

## 10. 本分支的 `ireclaim()`

经典 xv6 的“unlink 后保持打开”依赖最后一次 `iput()` 正常释放 inode。若系统在 `nlink` 已提交为 0、但打开引用尚未关闭时崩溃，内存 `ref` 丢失，重启后没有人触发该 `iput()`，会泄漏 dinode 和数据块。

本分支用 `ireclaim(dev)` 修复：

1. 顺序扫描 inode 1 到 `sb.ninodes - 1` 的 dinode。
2. 找到 `type != 0 && nlink == 0`，打印 `ireclaim: orphaned inode N` 并 `iget()`。
3. 为每个 orphan 单独 `begin_op()`。
4. `ilock()`/`iunlock()` 强制加载其字段，使 `valid == 1`。
5. `iput()` 在 `ref == 1 && nlink == 0` 条件下截断并把 type 清零。
6. `end_op()` 提交回收事务。

每个 inode 单独事务避免一次回收过多块超过日志容量。`ireclaim` 本身不验证目录可达性；它只处理明确由 `nlink == 0` 标记的孤儿。`test-xv6.py` 配合 `user/forphan.c`、`user/dorphan.c` 在破坏性 QEMU 崩溃后验证这条路径。

## 11. 原始块分配 `balloc()`

`balloc(dev)` 按 bitmap block 顺序扫描，从低块号到高块号寻找 0 bit：

```text
read bitmap block
find first zero bit within sb.size
set bit
log_write(bitmap buffer)
release buffer
bzero(new block)
return block number
```

新块先在 bitmap 中标记，再通过 `bzero()` 将数据清零；两次更新都加入同一外层事务，因此崩溃恢复不会暴露“已分配却含旧文件数据”的已提交状态。清零也让新间接块的所有地址自然为 0。

磁盘满时打印 `balloc: out of blocks` 并返回 0。块 0 被磁盘布局预先标记占用，所以 0 可以安全作为失败哨兵。

`bfree(dev, b)` 找到对应 bit，若本来为 0 则 panic，防止双重释放；否则清 bit 并 `log_write()` bitmap buffer。它不清除被释放块内容，保密性依赖下一次 `balloc()` 的清零。

## 12. 文件块映射 `bmap()`

每个 inode 有 12 个直接地址和 1 个间接块地址：

```text
logical blocks 0..11    -> ip->addrs[0..11]
logical blocks 12..267  -> uint entries in block ip->addrs[12]
```

`bmap(ip, bn)` 不只是查询；地址缺失时会调用 `balloc()` 分配：

- 直接块：分配后只更新内存 `ip->addrs[bn]`；预期的写路径最终由调用者 `iupdate()` 持久化，`readi()` 损坏洞反例则没有这个保证。
- 间接块：必要时先分配索引块并记录到 inode；再读索引块，必要时分配数据块并 `log_write()` 索引 buffer。
- 超过 `MAXFILE` 直接 panic。
- 空间不足返回 0，已经成功的早期分配可能仍保留并由后续 `iupdate()` 提交。

因为 `bmap()` 会分配，凡是不能事先保证映射存在的调用都必须持 inode 锁并位于事务中。`readi()` 也调用 `bmap()`，而普通 `fileread()` 没有 `begin_op()`；这条读路径完全依赖“`[0, size)` 内没有洞”的磁盘不变量，正常情况下 `bmap()` 只查询。

损坏镜像若在 `size` 内留下 0 地址，当前实现没有只读 lookup 或报损坏的分支，而会尝试 `balloc()`。若尚有空闲块且全局没有其他 outstanding 操作，`balloc()` 修改 bitmap 后第一次 `log_write()` 会因 `outstanding == 0` 触发 `panic("log_write outside of trans")`；若恰有别的线程处于事务中，由于 `log_write()` 只检查全局计数、并不验证当前线程，该读反而会无预留地借用对方的 group 完成分配。若磁盘已满，`bmap()` 返回 0，`readi()` 则以 0 或已读字节数短读。

即使 `readi()` 因调用者或并发操作而处于非零全局 `outstanding` 下，它自己也不调用 `iupdate()`：直接地址或新间接块地址未必会写回 dinode，甚至可能造成已登记 bitmap、却没有持久 inode 引用的块泄漏；已有间接块内的缺项则会被 `bmap()` 直接登记更新。所有这些都既未计入该普通读的预留，也不构成可靠修复。因此这不是稀疏文件支持，而是未防御的文件系统损坏路径。

## 13. 截断 `itrunc()`

调用者持 inode sleeplock 时，`itrunc()`：

1. 遍历 12 个直接地址，逐个 `bfree()` 并清零。
2. 若存在间接块，读取它并释放所有非零数据块。
3. 释放间接块本身并清 inode 的间接地址。
4. 设置 `size = 0`。
5. `iupdate()` 持久化空映射和大小。

该操作可调用 `bfree()` 很多次，但同一个 bitmap block 在一笔事务里只占一个日志槽。当前 `FSSIZE = 2000` 小于 `BPB = 8192`，整个镜像只有一个有效 bitmap block，所以截断最大文件仍只登记这一 bitmap home block 和 dinode 所在块；若扩大镜像到多个 bitmap blocks，必须重新核算 `MAXOPBLOCKS`。`O_TRUNC` 打开和最后一个 orphan 引用释放都会走这条路径。

## 14. `readi()`

`readi(ip, user_dst, dst, off, n)` 在 inode 已锁的前提下工作：

```text
reject off > size or unsigned off+n overflow by returning 0
clip n to size-off
for each covered block:
  addr = bmap(ip, off / BSIZE)
  if addr == 0: break
  bread(addr)
  copy min(remaining, bytes-to-block-end)
  brelse
return total
```

`user_dst` 决定 `either_copyout()` 的目标是用户虚拟地址还是内核地址。用户目标复制可能经 `copyout()` 为 lazy 区域补页；失败时函数释放 buffer 并返回 `-1`，即使此前块已成功复制，代码也把 `tot` 改成无符号的 `-1`，最终作为 `int` 返回 -1，而不是部分字节数。`copyout()` 按用户页推进，所以失败前不仅先前块、连失败这次跨页复制的前半段也可能已经写入用户内存，函数不会撤销这些字节。

越过 EOF 的读被截短，正好位于 EOF 返回 0。`off + n` 溢出也返回 0；这与 write 的 -1 不同。常规 `fileread()` 在持 inode 锁期间读取，并只在成功返回正数后增加共享 file offset。

## 15. `writei()`

`writei(ip, user_src, src, off, n)` 先拒绝：

- `off > ip->size`，因此不支持用 seek/空洞跨过 EOF；
- `off + n` 无符号溢出；
- 结束位置超过 `MAXFILE * BSIZE`。

循环中的顺序是 `bmap()` 分配/查询块、`bread()` 取得缓存、`either_copyin()` 把内核或用户源数据复制到块内，整次复制成功后才 `log_write()`。只有完整复制成功的这次 `m` 才会在循环尾计入 `tot` 和 `off`。最后若 `off` 越过旧 EOF 就扩大 `size`，并无条件 `iupdate()`：即使大小没变或 `tot == 0`，`bmap()` 也可能增加了 `addrs`。

这带来两个容易忽略的失败语义：

1. `copyin()` 按用户页复制。源区跨页且后一页无效或补页失败时，它可能已把前一页尾部复制进 `bp->data`，随后返回 -1；`writei()` 此时直接 `brelse()`，不会为这次失败再调用 `log_write()`，也不会恢复 buffer 原内容。若这是此前未登记的现有数据块，部分修改会先留在 cache，可被后续读看到，并可能在以后登记该块时被持久化，也可能在 buffer 被复用时丢失。若该块已在当前 group 中登记并被 pin，例如 `balloc()` 对新块执行 `bzero()` 时已经 `log_write()`，commit 会从同一 cache buffer 取最终版本，失败复制留下的前缀也会随事务写盘。
2. `bmap()` 发生在复制之前。新数据块即使一个用户字节都未完整写入，也已登记 bitmap 和清零后的数据块；函数退出前无条件 `iupdate()` 又会持久化新块地址。间接区只剩一个空闲块时，还可能先分配并最终持久化一个空的间接块，随后数据块分配失败。因此 `tot == 0` 不表示事务没有磁盘变化，失败路径也没有分配回滚。

空间不足或用户复制失败会让 `writei()` 返回已经完整完成的 `tot`，所以它可能部分成功；此前登记的变化会由外层事务提交。上层 `filewrite()` 将大请求切成最多 3072 字节的多次 `begin_op()`/`end_op()` 操作；这些分片在有并发 outstanding 操作时可能进入同一 group，并不是每片都独立 commit。任何分片短写都会让整个 `write()` 返回 -1，尽管 `filewrite()` 已增加 offset、先前完整分片和本分片的完整前缀仍可能保留。详见[一次文件读写](../flows/file-read-write.md)。

## 16. 目录就是定长记录文件

目录 inode 的内容是一串 `struct dirent`：

```c
struct dirent {
  ushort inum;
  char name[DIRSIZ];
};
```

`inum == 0` 表示空槽。名字最多 14 字节，恰好 14 字节时不以 NUL 结尾；因此比较必须使用 `namecmp()` 的 `strncmp(..., DIRSIZ)`，不能无界调用字符串函数。

`dirlookup(dp, name, poff)` 要求目录已锁且类型为 `T_DIR`。它逐记录 `readi()`，跳过空槽，匹配后可返回字节偏移，并通过 `iget()` 返回目标 inode 的未锁引用。父目录锁仍由调用者持有。

`dirlink(dp, name, inum)` 同样要求目录已锁：先确保同名项不存在，再寻找第一个空槽；没有空槽就在 `dp->size` 处追加。它用 `strncpy(..., DIRSIZ)` 固定填入名字，再 `writei()` 整个记录。磁盘满导致短写时返回 -1。

## 17. 路径元素截取 `skipelem()`

`skipelem(path, name)`：

- 跳过任意数量的前导 `/`；
- 复制下一个非斜杠区间；
- 再跳过后续重复 `/`；
- 返回剩余路径指针；没有元素时返回 0。

短于 14 字节的元素会显式 NUL 结尾；长度至少 14 时只复制前 14 字节。因此文件系统把超长路径元素按前 14 字节截断，两个相同前缀的长名字会冲突。这不是返回 `ENAMETOOLONG` 的 Unix 语义。

重复斜杠和尾随斜杠通常被折叠，例如 `///a//b/` 按 `a`、`b` 两个元素处理。`.` 与 `..` 没有语法特判，只是目录中由创建逻辑维护的普通条目。

## 18. `namex()` 路径遍历

`namex(path, nameiparent, name)` 选择起点：

```text
absolute path -> iget(ROOTDEV, ROOTINO)
relative path -> idup(myproc()->cwd)
```

每轮：

1. 锁住当前 inode。
2. 验证它是目录，否则 `iunlockput()` 失败。
3. 若请求 parent 且当前元素是最后一个，解锁但保留引用并返回当前目录，同时把末元素留在 `name`。
4. 否则 `dirlookup()` 取得下一 inode 引用。
5. 解锁并释放当前引用，把下一 inode 作为新当前项。

关键设计是绝不同时长时间持有父、子 inode 锁。`dirlookup()` 在父锁内只取得子引用；随后先释放父再进入下一轮锁子，避免沿路径形成任意锁链和父子反向死锁。

`namei(path)` 返回最终 inode；`nameiparent(path, name)` 返回父目录并给出最后一项。返回值都是“有引用、未锁”。仅 `/` 没有可返回的父和末元素，所以 parent 查询失败。

源码注释要求 `namex()` 位于事务内，因为失败或前进时的 `iput()` 可能发现 `nlink == 0` 并触发磁盘释放。系统调用和 `kexec()` 都相应建立事务边界。

## 19. `create()` 的两种路径

`kernel/sysfile.c:create(path, type, major, minor)` 首先 `nameiparent()` 并锁父目录。

若同名 inode 已存在：

- 请求类型为 `T_FILE` 且现有类型是 `T_FILE` 或 `T_DEVICE`，返回已锁的现有 inode；这支持 `open(O_CREATE)` 打开已有普通文件或设备。
- 其他组合失败，例如重复 `mkdir`、用普通创建覆盖目录。

若不存在：

```text
ialloc(type) -> unlocked referenced inode
ilock(new inode)
set major/minor/nlink=1; iupdate
if directory:
  add "." -> self
  add ".." -> parent
add parent/name -> new inode
if directory:
  parent.nlink++ for child's ".."
unlock+put parent
return new inode still locked and referenced
```

`.` 不额外增加新目录自己的 `nlink`，避免自引用计数环；父目录因子目录的 `..` 增加一次 nlink。

失败时把新 inode `nlink` 设为 0 并 `iupdate()`，再 `iunlockput()`；因为它是最后引用，`iput()` 会截断已经分配的目录块并将 type 清零。所有步骤在调用者事务中，所以不会提交“新目录项存在但新 inode 初始化一半”的命名状态。

父目录 nlink 只在所有目录项都成功后增加，减少回滚项。已写入新目录但未链接到父的中间状态不对其他路径可见，并在同事务失败清理。不过 `dirlink(parent, ...)` 仍继承 `writei()` 的分配不回滚语义：若父目录刚跨入间接区、只剩一个空闲块，`bmap()` 可先持久化一个空的父目录间接块，再因数据块分配失败而让 create 返回 0。新 inode 会清理，父目录这块未使用分配却会保留。

## 20. 硬链接 `sys_link()`

硬链接需要避免旧 inode 在新目录项建立前被并发删除，因此顺序是：

```text
begin_op
namei(old); lock old
reject T_DIR
old.nlink++; iupdate
unlock old but retain ref
nameiparent(new); lock parent
require same dev; dirlink(parent, name, old.inum)
unlock+put parent; put old
end_op
```

先增加 nlink 使旧 inode 即使原名字被并发 unlink 也不会释放。若新父路径不存在、跨设备或 `dirlink()` 失败，`bad` 路径重新锁旧 inode，撤销 nlink 并更新磁盘。这只回滚链接计数；若 `dirlink()` 已在父目录分配了空的间接块后才因数据块耗尽失败，该分配仍按 `writei()` 语义保留。

目录硬链接被禁止，否则用户可以制造目录环，使基于 `..` 的父关系、空目录判断和引用回收失去简单不变量。跨设备链接也被禁止，因为目录项只存 inode 号，不存目标设备号。

## 21. 删除 `sys_unlink()`

删除在一笔事务中锁住父目录并定位目标：

1. `nameiparent()` 返回父和最后名字。
2. 禁止名字 `.`、`..`。
3. `dirlookup()` 同时取得目标引用和目录项偏移。
4. 锁目标，验证 `nlink >= 1`。
5. 若目标是目录，`isdirempty()` 从第三个目录项开始确认除 `.`、`..` 外全为空。
6. 向父目录原偏移写一个全零 dirent，令名字立即不可查找。
7. 若删目录，父 `nlink--`。
8. 释放父锁/引用。
9. 目标 `nlink--`，更新并 `iunlockput()`。
10. 提交事务。

目标仍被打开时，最后一步只减 nlink，不截断，因为 `ref > 1`；最后一次 fileclose/cwd 替换才会释放。如果此间崩溃，启动 `ireclaim()` 负责清理。

非空目录拒绝删除。`isdirempty()` 假定前两个记录就是 `.` 与 `..`；它不重新验证两项内容，而是从 `2 * sizeof(dirent)` 开始扫描。

父锁先于子锁是创建/删除路径的统一顺序。禁止目录硬链接以及 `.`/`..` 删除有助于避免父子锁关系中的环。

## 22. `sys_open()` 的资源发布

`sys_open()` 复制路径和 mode 后先 `begin_op()`：

- 有 `O_CREATE`：调用 `create(T_FILE)`，得到已锁 inode。
- 无 `O_CREATE`：`namei()` 后锁 inode；目录只允许 `omode == O_RDONLY`。
- 设备 inode 的 major 必须在 `[0, NDEV)`。
- 分配全局 `struct file` 和当前进程最低空闲 fd；任一失败都回收已取得资源。
- 根据 inode 类型设 `FD_DEVICE` 或 `FD_INODE`，记录 inode、偏移、读写能力。
- `O_TRUNC` 且普通文件时调用 `itrunc()`。
- 解锁 inode，`end_op()`，返回 fd。

fd 在系统调用返回前已写入进程表，但当前 xv6 一个进程不会有另一个用户线程并发观察半初始化项。失败路径若已分配 fd，实际在本代码中 `fdalloc` 成功即不再有后续可报告失败；因此无需撤销已发布 fd。

本实现的模式边界需要按源码理解：

- `O_RDONLY` 为 0，目录检查要求 mode 必须恰好为 0；任何附加 bit 都拒绝。
- 对普通文件，`O_TRUNC` 没有强制要求可写，所以 `open(path, O_RDONLY | O_TRUNC)` 也会截断后返回只读 fd。
- 没有 `O_APPEND`；打开不截断文件时 offset 仍从 0 开始。
- `O_CREATE` 遇到已有设备 inode也可成功，最终创建 `FD_DEVICE`。

`O_CREATE` 对新路径的创建发生在 file/fd 分配之前。若 `create()` 已把一个新空文件链接进父目录，随后 `filealloc()` 或 `fdalloc()` 失败，错误路径只释放 file/inode 的内存引用，不会 unlink 刚建立的名字；`end_op()` 后 `open()` 返回 -1，但该空文件仍可存在并随 group commit 持久化。这是“资源获取失败不回滚已完成创建”的用户可见副作用。

## 23. `mkdir`、`mknod` 与 `chdir`

`sys_mkdir()` 和 `sys_mknod()` 都只是建立事务、复制参数、调用 `create()`，成功后 `iunlockput()` 返回的已锁 inode。`sys_mknod()` 先以 32 位 `int` 取得用户 major/minor，再因 `create()` 的 `short` 形参隐式窄化为 16 位有符号值并持久化；直到 open 时才检查窄化后的 major 是否在 `[0, NDEV)`，minor 是否有效则由具体设备语义决定。

`sys_chdir()`：

```text
begin_op
namei(new); ilock
require T_DIR
iunlock(new)
iput(old cwd)
end_op
p->cwd = new
```

新 inode 引用从 `namei` 转移给 `p->cwd`；旧 cwd 的引用在事务中释放，因为它理论上可能是已 unlink 的最后引用。`fork` 使用 `idup()` 复制 cwd，`exit` 在事务中 `iput()`，`exec` 保留 cwd。

## 24. 目录、打开文件与设备的边界

文件系统 inode 负责命名和持久元数据；打开后的 I/O 由 `kernel/file.c` 决定：

- `FD_INODE`：锁 inode，调用 `readi`/`writei`，更新共享 file offset。
- `FD_DEVICE`：绕过 inode 内容，通过 `devsw[major].read/write` 分派；inode 仍负责名字和设备号生命周期。
- `FD_PIPE`：不经过 inode 或磁盘文件系统。

打开目录得到 `FD_INODE` 且 readable，可用 `read()` 获取原始 dirent，`ls` 就这样遍历。写模式在 `sys_open()` 被拒绝，但内核内部 `dirlink`/`writei` 可修改目录。

`fileclose()` 释放最后一个 inode file 引用时会在自己的 `begin_op()`/`iput()`/`end_op()` 中完成潜在 orphan 删除。这是 `iput()` 事务前提跨到 file 层的关键连接。

## 25. 事务原子性与并发可见性

事务保证的是一个 group 中所有已登记磁盘块更新在崩溃后“全部安装或全部不安装”。对遵守日志协议、且由单次 `begin_op()`/`end_op()` 包围的受支持文件系统操作，这使该操作随整个 group 一起全有或全无；它不等价于任意用户系统调用都是单事务，也不让操作对其他 CPU 获得无锁隔离。并发可见性还依赖 inode 锁：

- 父目录锁串行化同目录的查找后创建、dirlink 和 unlink。
- 目标 inode 锁串行化 nlink、size、块映射和内容。
- buffer cache 保证每个磁盘块只有一个缓存副本。
- log 将多个 buffer 归入事务，并在没有 outstanding operation 时统一提交。

例如 `link` 会暂时先增加 nlink 再创建新目录项；其他进程可能在锁切换间看到 nlink 已增加，但找不到新名字。这个中间内存/缓存状态不会作为不一致的已提交磁盘事务暴露给崩溃恢复，且失败会在同事务撤销。

多个文件系统操作可同时 outstanding，它们的更新被当前简化日志合并成一次 commit；一个已经 `end_op()` 的操作甚至可能在 group 中还有其他 outstanding 操作时先返回，此时尚未承诺持久性。`begin_op()` 预留最坏块数，避免执行到一半才发现日志容量不足。大 `write()` 的多个分片是多次操作，可能跨 group，也可能因并发而连续加入同一 group，所以整个系统调用不具备全有或全无保证。

## 26. 失败语义汇总

| 失败 | 返回/行为 | 已有变化 |
|---|---|---|
| superblock magic 错误 | panic，系统不继续启动 | 无 |
| magic 正确但 superblock 区域、容量或 log header 损坏 | 没有统一拒绝路径；可能越界 I/O、panic 或错误安装 block | 镜像被当作可信 `mkfs` 输出，不能声称安全挂载任意损坏输入 |
| inode cache 槽耗尽 | panic | 引用持有者不变 |
| 磁盘块耗尽 | `balloc` 返回 0 | 当前写可能部分完成，先前分配不一定回滚 |
| dinode 耗尽 | `ialloc` 返回 0 | 本次扫描不分配 inode；create 释放父引用后失败 |
| `readi` 用户目标坏地址 | -1 | 之前复制到用户区的字节不会撤销 |
| `writei` 用户源坏地址或空间不足 | 返回完整完成的部分字节数，可能为 0 | 已登记块会提交；跨页失败还可能留下未再次 `log_write` 的部分 buffer 修改，分配也不回滚 |
| `filewrite` 任一 inode 分片短写 | -1 | offset 可能已前进，先前分片/当前完整前缀及失败分配可能保留 |
| 创建中途失败 | `create` 返回 0 | 新 inode 在同事务清理；父目录可能保留失败 `dirlink` 分配的空的间接块 |
| `link` 新目录项失败 | -1 | 预增 nlink 在同事务撤销；父目录失败分配不一定撤销 |
| `unlink` 非空目录或特殊项 | -1 | 目录项与 nlink 不变 |
| `open` file/fd 表耗尽 | -1 | inode/file 引用释放；`O_CREATE` 已新建的空文件不会 unlink |
| 路径中间项不是目录 | `namei` 返回 0 | 临时引用释放 |

xv6 没有 `errno`，多数用户可见错误统一为 -1。panic 表示内核内部不变量破坏或固定资源缓存耗尽，而不是普通输入错误。

## 27. 关键不变量

1. 对任意 `(dev, inum)`，最多一个 `ref > 0` 的内存 inode cache 项。
2. 访问 loaded inode 字段/内容必须持 `ip->lock`；改变磁盘字段后必须 `iupdate()`。
3. `nlink == 0 && ref == 0` 的已分配 inode不能长期存在；正常由 `iput` 清理，崩溃后由 `ireclaim` 清理。
4. bitmap 中已分配位必须覆盖所有 inode 引用的数据块和间接块；同一块不能属于两个文件。
5. `size` 范围内每个逻辑块应存在，当前实现不支持稀疏洞。
6. 目录有效项的 inode 号非零，名字按 14 字节比较；同一目录不得有两个同名有效项。
7. 除根目录外，每个创建成功的目录有 `.` 指向自己、`..` 指向父目录。
8. 目录硬链接被禁止，非空目录不能 unlink，因而目录结构保持无用户制造的环。
9. 可能释放或分配磁盘资源的路径必须在日志事务内。
10. 日志恢复必须先于 orphan 扫描。

## 28. 典型调用链

创建并写文件：

```text
sys_open(O_CREATE|O_TRUNC)
  -> begin_op -> create -> ialloc/dirlink -> filealloc/fdalloc -> end_op
sys_write
  -> filewrite chunks
     -> begin_op -> ilock -> writei -> bmap/balloc/log_write -> iupdate
     -> iunlock -> end_op
```

删除仍打开的文件：

```text
sys_unlink
  -> clear dirent + nlink--, ref remains > 1
  -> end_op; 名字在缓存中已不可查，随整个 group commit 后持久
process keeps using open struct file
sys_close/fileclose
  -> last file ref
  -> begin_op -> iput
  -> itrunc + type=0 -> end_op; 随整个 group commit 后持久
```

相对路径：

```text
namei("a/b")
  -> idup(cwd)
  -> lock cwd, dirlookup("a"), unlock+put cwd
  -> lock a, dirlookup("b"), unlock+put a
  -> return referenced, unlocked b
```

## 29. 验证和调试

运行级测试应覆盖：创建/覆盖/截断、大文件直接到间接块边界、磁盘满部分写、同目录并发创建、硬链接回滚、删除打开文件、空/非空目录删除、长名字截断、相对路径和 cwd 继承。

崩溃恢复测试由：

```sh
./test-xv6.py log
./test-xv6.py forphan
./test-xv6.py dorphan
./test-xv6.py crash
```

驱动。这些测试会强制杀死 QEMU 并复用磁盘镜像，不能与需要保存镜像状态的实例并行。

常用 GDB 断点：

```gdb
b fsinit
b begin_op
b log_write
b ialloc
b iput
b ireclaim
b bmap
b writei
b namex
b create
b sys_unlink
```

调试引用泄漏时同时观察 `ip->ref`、`ip->nlink`、`ip->valid`；调试磁盘块泄漏时观察 `ip->addrs`、bitmap buffer 和事务 header；调试路径死锁时记录已持有的父/子 inode sleeplock 顺序。

## 30. 阅读结论

本实现的核心不是某个单独查找函数，而是四组配对关系：dinode 与 inode cache、`nlink` 与 `ref`、inode sleeplock 与日志事务、目录项删除与延迟资源回收。只要沿每次操作追踪“名字、引用、锁、事务”四条线，就能解释文件为何可在 unlink 后继续使用、为何路径遍历不同时锁整条路径，以及为何本分支必须在日志恢复后额外运行 `ireclaim()`。
