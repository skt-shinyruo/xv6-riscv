# 文件系统与日志资源上界

本文把 `kernel/fs.c`、`kernel/sysfile.c` 和 `kernel/file.c` 中会修改磁盘的路径，转换成 `kernel/log.c` 实际计费的 **unique home block 集合**，再检查 `kernel/param.h` 中 `MAXOPBLOCKS=10`、`LOGBLOCKS=30`、`NBUF=30` 是否足够。

结论必须分层陈述：

- 对当前 `mkfs/mkfs.c` 生成的 `FSSIZE=2000` 镜像，并假定 superblock、文件系统结构和第 1 节的逐路径调用交错前提均成立，本文列出的正常日志操作区间最多登记 7 个 unique home blocks，所以小于 `MAXOPBLOCKS=10`。
- 这个结论强烈依赖当前镜像只有一个 bitmap block。同一 bitmap block 上的多次 `balloc()`/`bfree()` 被 absorption 后只占一个日志槽。
- `MAXOPBLOCKS=10` 不是任意 `FSSIZE`、任意布局下都成立的定理。`itrunc()` 一次释放整个文件；扩展到多个 bitmap blocks 后，它可以轻易超过 10。对当前实现，`writei()` 在任何 `bmap()` 之前拒绝 `off > ip->size`，所以 `O_TRUNC` 后保留旧文件偏移不能制造 hole；在当前的连续写约束下，一个 3072 字节 chunk 在多 bitmap 布局中的上界是 10。若以后允许稀疏写，必须重新计算。
- `NBUF == LOGBLOCKS` 本身也不是缓存安全证明。若日志真的 pin 满 30 个 home buffers，commit 获取第一个 log data buffer 时就会触发 `panic("bget: no buffers")`。当前配置的余量来自实际操作集合远小于 10，而不是来自两个常量相等。

日志的提交、恢复与崩溃窗口见[一次文件系统事务](../flows/filesystem-transaction.md)，buffer cache 和 VirtIO 见[存储栈](storage-stack.md)，inode 与目录语义见[文件系统](filesystem.md)。

## 1. 分析范围和源码地图

本文只计算物理 redo log 和 buffer cache 的块级资源，不讨论 inode table、全局 file table 或 VirtIO descriptor 的槽位上界。

| 源码 | 本文使用的事实 |
|---|---|
| `kernel/log.c` | `begin_op()` 的 admission 公式、`log_write()` absorption、pin、commit 临时 buffer |
| `kernel/bio.c` | `bget()` 的 victim 条件和无空闲槽时直接 panic 的行为 |
| `kernel/fs.c` | `balloc()`、`bfree()`、`ialloc()`、`iupdate()`、`bmap()`、`writei()`、`dirlink()`、`itrunc()`、`iput()` |
| `kernel/file.c` | `filewrite()` 的 3072 字节分片和 `fileclose()` 的日志边界 |
| `kernel/sysfile.c` | `create()`、`link`、`unlink`、`open(O_TRUNC)`、`mkdir` 的真实成功及回滚路径 |
| `kernel/param.h` | `MAXOPBLOCKS`、`LOGBLOCKS`、`NBUF`、`FSSIZE` |
| `kernel/fs.h` | `BSIZE`、`BPB`、`IPB`、直接/间接块数和块号映射宏 |
| `mkfs/mkfs.c` | 当前镜像的 log、inode、bitmap 和 data 区布局 |

所谓“一个操作”是一次匹配的 `begin_op()` 到 `end_op()` 区间，不一定等于整个用户系统调用。例如 `filewrite()` 会把一个大 `write()` 拆成多个这样的区间；反过来，多个并发区间又可能进入同一个 group transaction。

本文的正常路径还使用以下文件系统不变量：

1. superblock 来自当前 `mkfs`，各区域不重叠且 block number 位于镜像内；
2. inode 的非零 data block 地址互不重复，间接表格式正确；
3. 正常文件和目录在 `[0, size)` 内没有 hole；
4. 可由命名空间找到的普通 inode 具有与引用关系一致的 `nlink`；
5. `begin_op()`/`end_op()` 配对，调用者只在自己的区间内执行 `log_write()`；
6. 逐路径表假定没有另一个并发 `unlink` 让本区间正在释放的路径引用变成最后一个 orphan 引用。若该合法竞态发生，`namex()`/`iunlockput()` 可能在本区间额外触发 `iput()->itrunc()`，必须把被回收 inode 的 `I+A(F)` 并入集合。

这些不是全部由运行时校验强制的。尤其 `fsinit()` 只验证 `sb.magic`，并不验证 `sb.size`、`nlog`、各区域范围或重叠；损坏或手工构造的镜像不享有下文的当前布局上界。第 6 条也不是锁协议提供的全局保证；它只是把串行/无 orphan 回收的逐路径计数与“允许并发”的 group admission 推导分开。

## 2. 精确定义：日志到底对什么计数

对一个日志操作区间 `op`，定义：

```text
W(op) = { (dev, blockno) |
          该区间至少一次把这个 home buffer 传给 log_write() }
```

多个区间进入同一个 group 时，内存日志保存的是集合并：

```text
G = union W(op_i)
log.lh.n = |G|
```

`kernel/log.c:log_write()` 线性扫描 `lh.block[]`。相同 block number 已存在时不增加 `lh.n`，这就是 log absorption。因此计数不是：

- `log_write()` 的调用次数；
- 修改的字节数；
- 分配或释放的逻辑块数；
- 写入 on-disk log 时使用的 header/log data blocks 数量。

例如，截断一个大文件可能调用 `bfree()` 269 次，但若这些物理块的 bit 都位于同一个 bitmap home block，日志只新增一个 bitmap 项。反过来，一个数据块分配至少可能登记 bitmap block 和新数据块两个不同 home blocks。

日志键只比较 `blockno`，没有把 `dev` 放进 `logheader`；当前系统只有 `ROOTDEV` 上一个文件系统，所以本文仍写 `(dev, blockno)` 以表达概念边界。若改成同一全局日志跨多个设备，现有格式本身不足以区分同号块。

### 2.1 符号

| 符号 | 含义 |
|---|---|
| `I(x)` | inode `x` 所在的 dinode home block，即 `IBLOCK(x->inum, sb)` |
| `B(x)` | 物理块 `x` 的 allocation bit 所在 bitmap home block，即 `BBLOCK(x, sb)` |
| `D` | 文件或目录的一个数据 home block |
| `J` | inode 的 single-indirect home block；它位于 data 区，但内容是 block number 数组 |
| `A(S)` | `{ B(x) | x in S }`，一组分配/释放块所涉及的 unique bitmap blocks |
| `F(ip)` | `itrunc(ip)` 实际释放的所有物理块，包括 data blocks 和 indirect block 本身 |

不同 inode 可能落在同一个 `I(x)`，不同分配也可能落在同一个 `B(x)`。为了求最坏值，除非当前布局强制相同，表格都先假定它们不同；真实 alias 只会使集合更小。

## 3. 当前镜像为何只有一个 bitmap block

`kernel/fs.h` 给出：

```text
BSIZE     = 1024
BPB       = BSIZE * 8 = 8192 bits per bitmap block
NDIRECT   = 12
NINDIRECT = BSIZE / sizeof(uint) = 256
MAXFILE   = 12 + 256 = 268 data blocks
```

`struct dinode` 是 64 字节，所以 `IPB=16`。当前 `mkfs/mkfs.c` 使用 `NINODES=200`，并从 `kernel/param.h` 取得 `FSSIZE=2000`、`LOGBLOCKS=30`：

```text
nlog         = LOGBLOCKS + 1 = 31       # 1 header + 30 log data
ninodeblocks = NINODES / IPB + 1 = 13
nbitmap      = FSSIZE / BPB + 1 = 1
nmeta        = 2 + 31 + 13 + 1 = 47

logstart     = 2
inodestart   = 33
bmapstart    = 46
first data   = 47
```

对每个合法镜像块 `0 <= x < 2000`：

```text
B(x) = x / 8192 + bmapstart = 46
```

所以当前镜像中的任意次数分配与释放最终都只登记 bitmap block 46 一次。这是后续 2、4、7 等小上界的核心原因，不是 `bfree()` 次数本来就少。

需要区分编译常量和运行时事实：内核不会检查读到的 `sb.size` 等于 `FSSIZE`，也不会检查 `sb.bmapstart==46`。因此“只有一个 bitmap block”严格说是当前 `mkfs` 产物的性质，不是任意带正确 magic 的镜像都满足的内核不变量。

## 4. 原语的集合成本

### 4.1 inode、分配与释放

| 原语 | 新增集合 | 说明 |
|---|---|---|
| `iupdate(ip)` | `{I(ip)}` | 同一 dinode block 的重复更新被吸收 |
| `ialloc(dev,type)` | `{I(new)}` | 扫描是只读；只登记找到空 inode 的那个 block |
| `balloc(dev)` 成功返回 `x` | `{B(x), x}` | bitmap 置位后，`bzero()` 又登记清零后的新块 |
| `balloc(dev)` 返回 0 | `{}` | 扫描 bitmap 但没有修改，不调用 `log_write()` |
| `bfree(dev,x)` | `{B(x)}` | 只清 allocation bit，不登记或清零块 `x` 的旧内容 |

分配 `x` 后再向它写数据不会重复增加 `x`：`bzero()` 已先登记并 pin 同一个 buffer，后续 `writei()` 被 absorption。

### 4.2 `bmap()`

对一个尚未映射的 logical block：

| 情况 | `bmap()` 自身可能登记的集合 |
|---|---|
| 新 direct data block `x` | `{B(x), x}` |
| indirect block `J` 已存在，新 data block `x` | `{B(x), x, J}` |
| 首次进入 indirect 区，同时分配 `J` 和 `x` | `{B(J), J, B(x), x}` |

最后一行中 `J` 的清零、在 `J` 中写入 `x` 的地址都只占 `J` 一项。

如果 `J` 分配成功而 `x` 分配失败，`bmap()` 返回 0，但已经留下：

```text
{B(J), J}
```

调用它的 `writei()` 随后仍执行 `iupdate(ip)`，所以 inode 会持久化这个空 indirect block 地址。源码没有释放它。这一失败副作用会出现在 `create()` 和 `link()` 扩展父目录的 rollback 路径中。

### 4.3 `writei()` 与目录插入

一次成功 `writei(ip, ..., off, n)` 登记：

```text
实际写过的每个 data block
+ 新分配物理块对应的 unique bitmap blocks
+ 修改过的 indirect block（若有）
+ I(ip)
```

`writei()` 通过入口边界检查（包括 `off <= ip->size`）后，无论 size 是否改变、循环是否因 `bmap()==0` 提前退出，都会在尾部调用 `iupdate()`。因此“失败时 inode 内容没有变化”也不表示它不占日志槽；但若在入口因偏移、整数溢出或 `MAXFILE` 检查失败，则会在任何 `bmap()`/`log_write()` 前直接返回。

把 `dirlink(dp,...)` 的一次 16 字节插入记作 `L(dp)`。正常无 hole 的目录只有以下路径：

| 插入位置 | `L(dp)` 的集合 | 当前镜像上界 | 多 bitmap 镜像上界 |
|---|---|---:|---:|
| 复用已有 data block 中的空 dirent | `{D, I(dp)}` | 2 | 2 |
| 尾部新建 direct data block `D` | `{B(D), D, I(dp)}` | 3 | 3 |
| 已有 `J`，尾部新建 indirect data block `D` | `{B(D), D, J, I(dp)}` | 4 | 4 |
| 同时新建 `J` 和 indirect data block `D` | `{B(J),J,B(D),D,I(dp)}` | 4 | 5 |

当前最后一行是 4 而不是 5，因为 `B(J)==B(D)==46`。

## 5. `create(T_FILE/T_DEVICE)`

如果名字已存在，`create(path,T_FILE,...)` 对普通文件或设备 inode 直接返回已锁 inode；它本身不登记 home block。以下分析针对新名字。

### 5.1 成功路径

调用链为：

```text
ialloc(type)                 -> I(new)
iupdate(new fields/nlink)    -> I(new), absorbed
dirlink(parent,name,new)     -> L(parent)
```

所以：

```text
W(create-file-success) = {I(new)} union L(parent)
```

最坏情形是父目录第一次扩展到 indirect 区：

- 当前镜像：`1 + 4 = 5`；
- bitmap blocks 可分离时：`1 + 5 = 6`。

`T_DEVICE` 的创建成本相同；major/minor 只是同一个 dinode block 中的字段。

`sys_open(O_CREATE)` 在成功 `create()` 之后才调用 `filealloc()` 和 `fdalloc()`。这两个内存资源若耗尽，错误路径会释放引用，但不会 unlink 已创建的文件，因此 `open` 可以返回 `-1` 而上述最多 5 项仍被提交。

### 5.2 失败和清理

`ialloc()` 失败时没有写入。`ialloc()` 成功而父目录插入失败时，`create()` 把 `new->nlink` 改回 0；`iunlockput(new)` 可立即进入 `iput()->itrunc()`，最后再把 inode type 清零。同一个 `I(new)` 被多次更新但只占一项。

值得单独列出父目录刚进入 indirect 区的失败：

```text
allocate parent J succeeds   -> B(J), J
allocate parent data fails   -> no new item
writei tail iupdate(parent)  -> I(parent), records J pointer
rollback new inode           -> I(new), absorbed
```

当前和一般布局都是最多 4 项 `{B(J),J,I(parent),I(new)}`。rollback 只清理新 inode；父目录保留 size 之外的空 `J`，所以这不是“整个 create 没有副作用”。

## 6. `create(T_DIR)` 与 `mkdir`

`sys_mkdir()` 的磁盘工作全部由 `create(path,T_DIR,...)` 完成。新目录先建立自己的 `.`、`..`，再链接到父目录：

```text
ialloc + initialize new inode
dirlink(new, ".",  new.inum)
dirlink(new, "..", parent.inum)
dirlink(parent, name, new.inum)
increment parent.nlink
```

空目录的前两个 dirent 位于同一 data block `Dnew`。第一次 `dirlink` 分配并清零它，第二次只修改同一块；新 inode 的初始化、两次 size 更新也都落在 `I(new)`：

```text
Q(new-directory) = {I(new), B(Dnew), Dnew}
W(mkdir-success)  = Q(new-directory) union L(parent)
```

若父目录第一次扩展到 indirect 区：

- 当前镜像集合可写成 `{I(new),Dnew,B0,I(parent),Jparent,Dparent}`，共 6 项；
- 多 bitmap 布局中，`Dnew`、`Jparent`、`Dparent` 的 allocation bits 可位于三个不同 bitmap blocks，最多 8 项。

最后的 `parent->nlink++` 更新被 `L(parent)` 尾部的 `iupdate(parent)` 吸收。

失败时，只有 `.` 的首次 data block 分配可能让新目录初始化失败；一旦 `.` 写入成功，`..` 使用同一块且内核源地址复制不会失败。父目录插入失败后，新目录 inode 的 `nlink` 归零，`iput()->itrunc()` 释放 `Dnew`。若父目录已分配空 `Jparent` 后才耗尽：

- 当前镜像最多登记 `{I(new),Dnew,B0,Jparent,I(parent)}`，共 5 项；
- 两次分配可落到不同 bitmap blocks 时最多 6 项。

新目录的 data block 被回收，但父目录的空 indirect block仍保留。

## 7. `link`：成功与 rollback

`sys_link()` 先增加旧 inode 的 `nlink` 并登记 `I(old)`，然后才查找并锁定新父目录。这保证并发 unlink 不会在新目录项建立前释放旧 inode。

### 7.1 成功

```text
W(link-success) = {I(old)} union L(new-parent)
```

- 当前镜像最坏为 5；
- 多 bitmap 镜像最坏为 6。

如果 old inode 与 parent inode 恰好位于同一个 dinode block，或新链接复用已有目录块，实际集合更小。

### 7.2 rollback

新父路径不存在、跨设备、名字已存在、目录已满或分配失败时，`bad` 分支把 `old->nlink` 减回原值并再次 `iupdate(old)`。第二次更新被 absorption，所以 `I(old)` 仍占一个槽，即使隔离观察时最终字节与操作前相同。

大多数早期失败只登记 `{I(old)}`；父目录新 direct block 分配直接失败时，`writei()` 还会登记一次内容未改变的 `I(parent)`。最重的 rollback 仍是“父目录 `J` 分配成功、data block 分配失败”：

```text
W(link-rollback-partial-J)
  = {I(old), B(J), J, I(parent)}
```

最多 4 项。旧 inode 的 `nlink` 被还原，但空 `J` 和它的 allocation bit 不会回滚，因此失败的 `link` 也可能消耗磁盘块。

## 8. `unlink`：是否触发最终回收是分界线

成功 `unlink` 已找到一个位于现有父目录 data block 的 dirent，因此清零 dirent 不会分配块：

```text
clear parent dirent via writei -> Dparent, I(parent)
decrement target.nlink         -> I(target)
```

若目标仍有其他硬链接或内存引用，`iunlockput(target)` 不截断：

```text
W(unlink-no-reclaim) = {Dparent, I(parent), I(target)}
```

最坏 3 项。目标为目录时，`parent->nlink--` 仍落在已经登记的 `I(parent)`。

若这次 `iput()` 同时看到 `ref==1 && nlink==0`，它在同一个日志区间内调用 `itrunc()` 并清除 inode type：

```text
W(unlink-with-reclaim)
  = {Dparent, I(parent), I(target)} union A(F(target))
```

当前所有 `B(x)` 都是 block 46，所以非空目标最坏 4 项。目标可以是曾经增长很大、后来所有普通 dirent 都被清空的目录；`isdirempty()` 只检查 dirent，不释放多余 data blocks，因此最终 directory unlink 也可能让 `itrunc()` 循环释放大量块，但当前日志项仍因 bitmap absorption 保持为一个。

失败路径在本文的隔离假设下只读目录和 inode，不登记 block。损坏的 `nlink/ref` 状态会让任意 `iput()` 意外进入 `itrunc()`；此外，第 1 节第 6 条所述的合法并发 `unlink` 也可能让一个路径引用成为 orphan 的最后引用。这两类情况都要额外并入被回收 inode 的 `I+A(F)`，不属于本表的逐路径隔离上界。

## 9. `iput()`、`itrunc()` 与 `O_TRUNC`

一个最大文件有 268 个 data blocks；若使用 indirect 区，还要释放 indirect block 本身，因此：

```text
|F(ip)| <= NDIRECT + NINDIRECT + 1 = 269
```

`itrunc()` 不把被释放 data/indirect block 的旧内容写入日志，只逐个调用 `bfree()`，最后用 `iupdate()` 把 size 和地址清零：

```text
W(itrunc(ip)) = A(F(ip)) union {I(ip)}
|W(itrunc)|   = |A(F(ip))| + 1
              <= min(269, number-of-reachable-bitmap-blocks) + 1
```

当前 `A(F(ip))` 至多 `{46}`，所以：

- `F(ip)` 为空，即 direct/indirect 地址中没有任何待释放块时，`itrunc()` 只登记 1 项 `I(ip)`；
- `F(ip)` 非空时最多登记 2 项 `{46,I(ip)}`。不能用 `ip->size==0` 代替这个条件：`writei()` 可能先经 `bmap()` 分配块，随后因用户 `copyin()` 失败而不推进 size，却仍在尾部 `iupdate()` 中持久化新块地址。

`iput()` 回收 orphan 时，随后把 type 清零并再次 `iupdate()`，仍是同一个 inode block，不增加上界。这个路径可由最后一次 `fileclose()`、进程释放 cwd、`unlink` 的最后引用以及启动时 `ireclaim()` 触发。

`sys_open(O_TRUNC)` 在自己的 `begin_op()`/`end_op()` 内直接调用同一个 `itrunc()`，没有分片，因此对已有文件也是当前最多 2 项。`O_CREATE|O_TRUNC` 新建空文件时，截断只重复登记创建阶段已有的 `I(new)`；若名字已存在，则按普通 `O_TRUNC` 计算。

### 9.1 扩大镜像后的反例

只修改 `FSSIZE` 或换入更大镜像后，不能继续引用“最多 2 项”。考虑一个合法最大文件，其中至少 10 个 data blocks 分散在 10 个不同 bitmap regions：

```text
B(data_0), B(data_1), ..., B(data_9) are all different
```

一次 `itrunc()` 至少登记这 10 个 bitmap blocks 和 1 个 dinode block，共 11 项，超过 `MAXOPBLOCKS=10`。在 `unlink` 最终回收中还要并入父目录 data、父 inode 等项。

形成 10 个 bitmap regions 需要磁盘覆盖相隔 `BPB=8192` 的物理块范围，但 file 只需在每个范围拥有一个块；碎片化分配足以做到，不要求单个文件连续覆盖数万块。这里是一个“可构造的合法布局”反例，而不是声称只把 `FSSIZE` 改大、继续使用当前 `NINODES=200` 的顺序 `mkfs` 就必然得到该布局；后者还受 inode 总数和 allocator 的 first-fit 顺序限制。可通过同时提高 inode 容量、预先制造碎片或手工构造并校验镜像来实现。当前实现没有：

- 按 bitmap block 分片的 truncate；
- truncate 进度或可恢复游标；
- 在 `itrunc()` 内检查本区间已经使用多少日志项；
- 建镜像或启动时验证 `FSSIZE/BPB` 与 `MAXOPBLOCKS` 的关系。

所以 `itrunc()` 对一般布局的安全性无法从当前常量关系证明。

## 10. 一个 write chunk 的严格推导

`kernel/file.c:filewrite()` 计算：

```text
max = ((MAXOPBLOCKS - 1 - 1 - 2) / 2) * BSIZE
    = ((10 - 1 - 1 - 2) / 2) * 1024
    = 3072 bytes
```

每个 chunk 单独执行 `begin_op()`、`writei()`、`end_op()`。设：

- `T` 是本 chunk 跨越的 file data blocks 数；
- `M` 是本 chunk 新分配的物理块集合，包括新 data blocks 和可能新建的 `J`；
- `j=1` 表示本 chunk 新建或修改并登记了 indirect block，否则为 0；即使随后 data block 分配失败而没有建立数组项，已经清零的 `J` 仍算在内。

任意起始偏移下，3072 字节最多跨 4 个 data blocks，因此 `T<=4`。集合公式为：

```text
W(write-chunk)
  subset of {I(file)} union touched-data-blocks union A(M) union {J if j=1}

|W| <= 1 + T + |A(M)| + j
```

### 10.1 当前镜像

当前只要 `M` 非空，`A(M)={46}`；无论 chunk 中分配 1 个还是 5 个物理块都只算一个 bitmap 项。因此：

```text
|W| <= 1 inode + 4 data + 1 bitmap + 1 indirect = 7
```

这是本文所有列举路径中的最大当前上界。新块在 `bzero()` 时已经登记，所以即使随后的 `copyin()` 失败，资源计数也不会超过这个集合；失败写不会自动释放已经分配的块。

### 10.2 无 hole 的一般布局

若写偏移不超过当前 size 且 `[0,size)` 没有 hole：

- 起始偏移不对齐时，第一块已经存在；4 个 touched blocks 中最多新分配后 3 个 data blocks；
- 起始偏移对齐时，3072 字节只触及 3 个 data blocks；
- 首次跨入 indirect 区可能再分配 1 个 `J`。

即使每次分配落在不同 bitmap block：

```text
unaligned: 1 inode + 4 data + 4 bitmap + 1 J = 10
aligned:   1 inode + 3 data + 4 bitmap + 1 J = 9
```

因此 `MAXOPBLOCKS=10` 对这个一般布局上界是足够的。这里的 dense-file 前提不是文档臆设：`kernel/fs.c:writei()` 明确检查 `off > ip->size` 并在此时返回，正常写入又只把连续成功的前缀推进到 `ip->size`。源码中的 `-1 -1 -2` 可以理解为给 inode、indirect 以及边界/分配余量留预算；若未来改成允许 sparse write，必须重新证明这个公式。

### 10.3 `O_TRUNC` 后的旧 offset 不会产生当前 hole

xv6 没有 `lseek`，但 open file description 的 `f->off` 不会随另一个 fd 的 `O_TRUNC` 归零：

1. fd A 顺序写到 `11*BSIZE+1`，使 A 的 `f->off` 保留在该非对齐位置；
2. fd B 打开同一 inode 并执行 `O_TRUNC`，inode size 和地址归零；
3. fd A 再写一个 3072 字节 chunk。

`filewrite()` 把旧的 `f->off` 传给 `writei()`，而 `kernel/fs.c:535-538` 的入口检查发现 `off > ip->size` 后立即返回 `-1`。因此步骤 3 不会调用 `bmap()`，不会分配 data/indirect block，也不会新增日志项。旧 offset 虽然没有被 `O_TRUNC` 修正，但当前 ABI 没有 `lseek`，这个状态不能绕过该检查制造稀疏文件。

如果未来删除这项检查并正式支持 sparse write，前述序列才可能在碎片化的大镜像中产生 4 个 data block、一个 `J` 和 5 个 bitmap block（共 11 项）；那是未来算法的反例，不是当前 xv6 的证据。

## 11. 操作上界总表

下表是第 1 节第 6 条前提下的**隔离路径表**，不包含释放路径引用时由并发 `unlink` 触发的额外 orphan 回收；发生该竞态时必须另并入 `I+A(F)`。表内按 dinode blocks 可彼此分离计算，同块 alias 只会降低结果。`b=|A(F(target))|` 表示 truncate 实际跨越的 bitmap blocks 数。

| 单个日志操作区间 | 当前 `mkfs` 镜像 | 多 bitmap 布局 | 关键前提 |
|---|---:|---:|---|
| 新建 `T_FILE/T_DEVICE` 成功 | 5 | 6 | 父目录可首次进入 indirect 区 |
| `mkdir` 成功 | 6 | 8 | 新目录 data 与父目录两次分配的 bitmap 可分离 |
| `link` 成功 | 5 | 6 | 含 old inode nlink 与父目录插入 |
| `link` 最重 rollback | 4 | 4 | 父目录只成功分配空 `J` |
| `unlink`，不最终回收 | 3 | 3 | 清 dirent、父/目标 dinode |
| `unlink`，同时最终回收 | 4 | `3+b`，可大于 10 | 当前 `b<=1`；一般 `b<=269` |
| 独立 `iput/itrunc/O_TRUNC` | 2 | `1+b`，可大于 10 | 当前 `b<=1` |
| 3072-byte write chunk | 7 | 当前连续写约束下 10 | `writei()` 拒绝 `off > ip->size`；当前所有 allocation bitmap 相同 |

表中当前最大值 7 小于 `MAXOPBLOCKS=10`。在上述隔离前提下，它覆盖 `create/mkdir/link/unlink/itrunc/O_TRUNC/filewrite` 的正常及已列失败路径；纯路径查找和正常 exec 读取不登记日志块。

这仍不是对任意内核状态的无条件证明。`readi()` 会调用 `bmap()`；若损坏 inode 在 `[0,size)` 内含 hole，未建立事务的 read 甚至可能调用 `log_write()`。`log_write()` 只检查全局 `outstanding>=1`，不验证当前线程拥有预留；若恰有另一个区间 outstanding，异常 read 可把未预留更新塞进其 group。类似地，若允许第 6 条所述的并发 orphan 回收，多 bitmap 布局中的额外 `I+A(F)` 也可能使表中 rollback/路径操作超过表内数值；这正是需要重新建模调用交错的边界，而不是当前静态集合表的反例。

损坏 executable 是另一个不同的 hole 入口：`kexec()` 已经为路径和 inode 读取调用 `begin_op()`，所以它不会借用别人的区间；但这次准入只预留 `MAXOPBLOCKS=10`。ELF/segment 读取可跨越许多 hole，每个新零数据块都会增加 unique log item，bitmap 和间接块还要占项，`readi()` 又不把新直接地址 `iupdate()` 回 dinode。于是 malformed sparse inode 可以在一次“读取区间”中超过 10，最终触发日志或 buffer cache panic，并留下已登记分配副作用。表中的“正常 exec 不登记”必须保留 dense、良构 inode 前提。

## 12. `MAXOPBLOCKS` 与 `LOGBLOCKS` 的条件证明

令：

```text
M = MAXOPBLOCKS = 10
L = LOGBLOCKS   = 30
n = current log.lh.n
o = current log.outstanding
```

`begin_op()` 只在下式成立时接受新操作：

```text
n + (o + 1) * M <= L
```

如果每个已接受区间在整个生命周期内新增的 unique block 总数确实不超过 `M`，这条检查是保守的：`n` 已含在途操作此前登记的块，但公式仍为每个 outstanding 操作重新保留完整 `M`。因此当前 group 能在所有参与者完成前容纳它们的集合并。

空 group 中 `n=0,o=0`：

```text
first begin_op:   0 + 1*10 <= 30
second begin_op:  0 + 2*10 <= 30
third begin_op:   0 + 3*10 <= 30
fourth begin_op:  0 + 4*10 >  30, wait
```

所以“3”是空日志下可同时准入的最坏预算数，不是一个 group 最多只能包含三个系统调用。只要至少一个旧区间仍 outstanding，已完成区间的内容会留在 `lh`，之后仍可能按公式再准入其他区间。

这项正确性是条件式的，原因有三：

1. 代码没有 per-operation 计数器，超过 10 的区间不会在第 11 项被识别为“本操作超额”；
2. `log_write()` 没有线程所有权，只要全局任意区间 outstanding，未调用 `begin_op()` 的线程也能登记；
3. 上文已给出多 bitmap `itrunc()` 的 `|W|>10` 反例；若未来允许 sparse write，write-chunk 也必须重新计算。

还有一个精确边界：`log_write()` 在扫描是否已存在之前先执行 `if (lh.n >= LOGBLOCKS) panic`。若 group 恰好已有 30 个 unique blocks，下一次即使只是重写其中一个 block，也会先 panic。因此“最终集合不超过 30”对任意调用序列仍不够；要么保证在最后一次 `log_write()` 之后才第一次达到 30，要么让实际集合保持严格低于 30，或者修改检查顺序。

## 13. 当前路径对 group pin 数的条件余量

每个首次登记的 home buffer 被 `bpin()` 一次，normal install 后才 `bunpin()`；在每次 `log_write()` 完成到 install 开始之间，提交前集合满足：

```text
number of log pins = |G| = log.lh.n
```

当前表格给出单区间实际上界 `A=7`，而 admission 仍按 `M=10`。可以在前述正常状态假设下进一步得到一个 group 的保守手工上界。

group 的第一个操作从干净的 `n=0` 开始，最多产生 7 项。取该 group 的**最后一次后续 admission**（如果没有后续 admission，初始批次最多是 `3*7=21`）；该次准入后共有 `k=o+1` 个 active 区间。由于在 `o==0` 时 `end_op()` 会立即进入 commit，后续 admission 必有 `k>=2`。准入瞬间：

```text
n + k*M <= L
```

即使把每个 active 区间已计入 `n` 的部分忽略，再为它们各加完整 `A`，完成后的集合也至多：

```text
|G| <= n + k*A
     <= L - k*(M-A)
     <= 30 - 2*(10-7)
     = 24
```

因此在“所有区间确实来自上表、当前单 bitmap 镜像、无异常无主 `log_write()`”的条件下，可推得提交前最多 24 个 pin，而不是 30。24 是保守人工上界，不表示代码实际维护了 per-group pin 计数，也不要求该值可达。

这个 24 是文档对当前调用图的推导，不是内核维护或检查的运行时不变量。增加新日志路径、改变 write 分片、换镜像布局或破坏事务所有权后必须重新证明。

## 14. `NBUF=30` 为什么不能只看等式

`kernel/bio.c:bget()` 只能回收 `refcnt==0` 的 buffer。所有 logged home buffers 都有 pin ref，因此不可回收；没有 victim 时它不睡眠等待，而是立即：

```text
panic("bget: no buffers")
```

normal commit 的 `write_log()` 对每个条目先 `bread(log.start+i+1)`，再 `bread(home)`。home buffer 因 pin 必然仍在 cache，第二次 bread 不需新槽，但 log data block 至少需要一个非 pinned 槽。因此最低条件包含：

```text
NBUF >= maximum-pinned-home-buffers + 1
```

普通文件系统路径执行期间也会有临时 buffer。例如 `bmap()` 可持有一个尚未登记的 indirect buffer，同时让 `balloc()` 读取 bitmap 或清零新 data block；所以完整条件应写成：

```text
NBUF >= max over execution points (pinned slots + all simultaneously live non-pinned slots)
```

常量 `NBUF=LOGBLOCKS=30` 没有编码右侧的 transient 项。一个直接的协议级反例是：

1. 三个获准区间各登记 10 个彼此不同的 home blocks；
2. `lh.n=30`，全部 30 个 cache buffers 被 pin；
3. 最后一个 `end_op()` 调用 commit；
4. `write_log()` 获取第一个 log data block，没有 `refcnt==0` 的槽，`bget()` panic。

这条序列符合 admission 的名义 10-block 预算，说明 `NBUF==LOGBLOCKS` 不能支撑“日志可安全用满”的说法。

在只考虑本文列出的事务路径、且没有并发非日志 cache 使用者时，还可以把 transient 槽算完整。单个 active 区间同时持有的非 pinned buffer 最多为 2：峰值发生在 `bmap()` 或 `itrunc()` 已持有 indirect buffer，又进入 `balloc()`/`bfree()` 取得 bitmap 或新 data buffer 时；其他列举路径不超过这个数。结合 admission 与每区间 `A=7`：

| 执行点 | pinned home slots 上界 | 同时 live 的非 pinned slots 上界 | 合计 |
|---|---:|---:|---:|
| `outstanding=3` | 21 | `3*2=6` | 27 |
| `outstanding=2` | 24 | `2*2=4` | 28 |
| `outstanding=1` | 24 | 2 | 26 |
| normal commit | 24 | 1 个 log/header buffer | 25 |

`outstanding=3` 的 21 来自第三个 admission 只能在当时 `n=0` 时成功；三个区间随后各至多增加 7 项。`outstanding=2` 若由新 admission 形成，则当时 `n+2*10<=30`，随后两区间至多再加 `2*7`，得到 24；若只是三个区间完成一个而降为 2，上界反而仍是 21。`outstanding=1` 使用上一节已经证明的整个 group 24 上界。normal commit 开始时已经没有 active 区间，逐项安装只需一个额外的 log/header 槽，home buffer 已计入 pin。

因此 30 个槽足以覆盖这个隔离调用集，局部峰值上界为 28；但这不是整个系统的 NBUF 证明。普通 `fileread()` 不调用 `begin_op()`，`fstat` 的首次 `ilock()` 也可能在区间外 `bread()` inode；这类并发 reader 可能在等待 I/O 或 copyout 时占用剩余槽，commit 仍可能在其间执行。`exec` 和当前系统调用中的路径读取虽有外围 `begin_op()`，admission 也只预留日志项而不预留非 pin buffer。恢复发生在其他进程启动前，没有旧 pin 或并发 reader；`install_trans(1)` 同时持有 log copy 和 home 两个 non-pinned buffers，30 个槽对该恢复局部峰值足够。

必须保持措辞边界：源码没有 `static_assert`、启动检查或动态 backpressure 来验证“pin<=24”或 transient headroom。一旦单操作 7 项上界不再成立，`bget` 风险会重新出现；不能因为当前测试未触发 panic 就把 `NBUF=30` 当作一般证明。

## 15. 三类可证明性结论

### 15.1 可直接由机制严格推出

- `log.lh.n` 计数 group 内 unique home block numbers，重复 `log_write()` 会 absorption；
- 成功 `balloc(x)` 登记 bitmap block 和清零后的 `x`，`bfree(x)` 只登记 bitmap block；
- 最大 inode 含 268 个 data blocks，`itrunc()` 最多调用 269 次 `bfree()`；
- admission 使用 `n+(o+1)*10<=30`，空 group 最多同时准入三个 10-block 预算；
- 首次登记的 home buffer 会一直 pin 到 normal install；`bget()` 没有空槽时 panic；
- 当前 log header 数组有 30 项，当前 `mkfs` 为它分配 30 个 log data blocks 加一个 header。

### 15.2 依赖当前 `mkfs` 布局和正常状态

- `FSSIZE=2000<BPB=8192` 使所有合法块共享 bitmap block 46；
- `itrunc/O_TRUNC` 最多 2 项，带最终回收的 `unlink` 最多 4 项；
- 当前逐路径最大是 write chunk 的 7 项，故 `MAXOPBLOCKS=10` 有 3 项单操作余量；
- 在没有并发非日志 cache 使用者时，隔离事务调用集的 group pin 最多 24、pin 加 transient buffer 的局部峰值最多 28；这不覆盖全系统 reader；
- 正常 read/path traversal 不分配、不登记日志块。

### 15.3 当前实现不能提供的一般保证

- superblock 与内核编译常量、区域范围和 `nlog>=LOGBLOCKS+1` 一致；
- 扩大到多 bitmap 文件系统后 `itrunc/unlink` 仍不超过 10；
- 不能保证损坏的 sparse inode 或未来允许 `off > ip->size` 的实现中，任意 file offset/布局下 3072 字节 chunk 都不超过 10；当前 `writei()` 的入口检查和连续写约束只支持前述合法 dense 情形的 10 项推导；
- 不能保证 `kexec()` 在损坏 sparse executable 上仍是零日志项或不超过自己的 10 项预留；事务边界存在不等于分配型 `readi()` 的最坏集合已经受控；
- 每个线程只能消费自己的 `begin_op()` 预留，或单区间实际不超过 10；
- 日志达到 30 项后重复登记已有 block 不 panic；
- `NBUF==LOGBLOCKS` 足以应对日志满容量、并发 reader 以及所有临时 buffer；
- allocation/write 失败会把已经修改的 bitmap、inode、data 和空 indirect block 全部回滚。

## 16. 修改参数或算法时如何重算

修改 `FSSIZE`、`BSIZE`、`MAXFILE`、`MAXOPBLOCKS`、`LOGBLOCKS`、`NBUF`，或给文件系统增加 double-indirect、extent、rename、稀疏文件支持时，至少重新完成以下检查：

1. 对每个 `begin_op()/end_op()` 区间列出所有可达 `log_write()`，用 block number 集合而非调用次数计数；
2. 对每个 allocation/free 集合计算它们最多横跨多少个 `BBLOCK()`；不要默认 bitmap absorption；
3. 把成功、部分分配、copy failure、rollback 和最后引用触发 `iput()` 分开；
4. 证明每个区间 `|W(op)|<=MAXOPBLOCKS`，或给长操作实现可恢复分片；
5. 用 admission 公式证明 group 的最大 pin 数，不能简单使用 `LOGBLOCKS` 作为实际 pin 数；
6. 逐控制点计算 pin 加临时 buffer 的峰值，要求严格不超过 `NBUF`；
7. 验证 `MAXOPBLOCKS>0`，使预留预算有意义；再验证 `LOGBLOCKS>=MAXOPBLOCKS`，否则干净 group 中的首个操作会永远无法准入；当前 `filewrite()` 还要求 `MAXOPBLOCKS>=6` 才能得到正 chunk；
8. 验证 `sizeof(logheader)<BSIZE`、磁盘 `nlog>=LOGBLOCKS+1`，并重建 `fs.img`；在当前 4 字节 `int` 和 `BSIZE=1024` 下，前一条件要求 `LOGBLOCKS<=254`；
9. 增加能制造边界布局的测试，而不只运行顺序分配、单 bitmap 的默认镜像。

若要让这些关系从“人工证明”升级为实现保证，优先改进方向是：启动时验证 superblock 布局、按操作跟踪日志配额、让 `log_write()` 先做 absorption 再判断新增容量、为 truncate 设计分片协议，以及令 buffer cache 在暂时无 victim 时等待或明确保留 commit headroom。

## 17. 静态复核方法

以下命令不会启动 QEMU，也不会修改 `fs.img`：

```sh
rg -n 'MAXOPBLOCKS|LOGBLOCKS|NBUF|FSSIZE' kernel/param.h
rg -n 'begin_op|end_op|log_write|bpin|bunpin' kernel/log.c kernel/file.c kernel/sysfile.c kernel/fs.c
rg -n 'balloc|bfree|bmap|itrunc|writei|dirlink' kernel/fs.c
```

这些检查能发现常量和符号漂移，但不能验证集合证明。代码审阅时还应手工把新增 `log_write()` 放回本文相应的 `W(op)`；若修改镜像布局，应记录新的 `nbitmap` 并构造跨 bitmap 的 truncate/write 压力用例。
