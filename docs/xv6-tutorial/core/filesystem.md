# 文件系统命名、inode 与数据路径

## 问题场景与本单元成果

在 `mkdir("fsd")` 后执行 `open("fsd/data", O_CREATE | O_RDWR)` 并不只创建一个“文件”。它先从 cwd inode
逐段解析 pathname，在父目录的数据中写入 `struct dirent`，分配并锁定 inode，再通过
`struct file` 暴露给 descriptor。随后写入 `12305` 字节会越过 `NDIRECT=12` 个 direct
block：前 `12288` 字节占满 12 个 block，最后 17 字节需要一个 indirect metadata block
和第一个 indirect data block。读取则沿同一 inode mapping 回到准确的字节序列。

本单元的主要成果是能把 image construction、namespace、inode ownership、block mapping 与
read/write 串成一条可复核路径，并能解释 link、unlink、open reference、`O_TRUNC` 和 allocation
failure 的实际副作用。本单元唯一出口是一个“文件系统命名与数据路径报告包”；源码追踪
worksheet、raw marker、资源账本、回归与局限都嵌在这个报告内，不另交第二份产物。

## 前置单元与暂存黑盒

硬前置是[设备中断与 VirtIO 队列](device-io.md)：本单元从 `bread()` 已取得、最终由
`brelse()` 归还的 `struct buf` 开始，不重复 descriptor ring 或 interrupt completion。
相关背景包括[文件描述符、管道、控制台与设备 I/O](communication-and-io.md)、
[进程生命周期与回收](process-and-memory.md)和
[调度、同步与等待](scheduling-and-synchronization.md)。

本单元把 `bread()`、`brelse()` 和 `log_write()` 当作可见边界，但不解释 buffer cache 的
one-buffer-per-block、LRU/eviction、pinning、log reservation、commit、home-block installation
或 recovery。`begin_op()/end_op()` 只标记 filesystem operation 的外框，不构成持久化
oracle；这些黑盒由后续 `core.persistence`（#16）解除。因此本单元不声称 crash atomicity、
host durability、任意并发 pathname interleaving 或 cache scalability，`C=N/A`、`R=N/A`。

## 最小模型和关键不变量

### Image 与 on-disk layout

`Makefile:fs.img` 运行 `mkfs/mkfs`，把 `README` 和 `UPROGS` 写入一个新的 image。
`mkfs/mkfs.c:main` 计算 `nmeta` 和 `nblocks`，清零 `FSSIZE` 个 block，在 block 1 写入
`struct superblock`，建立 `ROOTINO` 及 `.`、`..`，再由 `balloc(freeblock)` 写 bitmap。
`kernel/fs.h` 给出共享格式：

| 区域或对象 | 定位规则 | 本单元检查的事实 |
|---|---|---|
| superblock | block 1 | `magic==FSMAGIC`，并记录 `size/nblocks/ninodes/nlog/logstart/inodestart/bmapstart` |
| inode block | `IBLOCK(inum, sb)` | `struct dinode.type!=0` 表示 on-disk allocation |
| allocation bitmap | `BBLOCK(blockno, sb)` | bit=1 表示 block 已分配；它与 inode 数分别记账 |
| directory data | `struct dirent { inum, name[DIRSIZ] }` | name 只是到 inode number 的映射，不拥有打开引用 |
| file data | `addrs[0..NDIRECT-1]` 与 `addrs[NDIRECT]` | `BSIZE=1024`，最多 `MAXFILE=268` 个 data block |

`mkfs:ialloc/iappend` 是 host-side image builder；runtime 的 `fs.c:ialloc/bmap` 是 kernel
实现。两边共享 layout，却不是同一套 allocator，也不能用 host builder 的顺序替代 runtime
证据。

### Namespace 与 inode ownership

`namex()` 对 absolute path 从 `(ROOTDEV, ROOTINO)` 开始，对 relative path `idup(cwd)`；
每一段先 `ilock(ip)`，确认 `T_DIR`，再经 `dirlookup()` 取得下一个 `iget()` reference，最后
`iunlockput()` 当前 inode。`nameiparent()` 在最后一段前停止，把仍被引用但已解锁的 parent
和 `name[DIRSIZ]` 交给 `create()`、`sys_link()` 或 `sys_unlink()`。

必须区分四类状态：

| 状态 | 所在位置 | 含义与保护 |
|---|---|---|
| `struct dinode.type/nlink/size/addrs` | disk inode block | on-disk allocation、namespace link 数和数据映射 |
| `struct inode.ref/dev/inum` | `itable` | 内存指针身份；`itable.lock` 保护 slot identity/ref |
| `struct inode.valid/type/nlink/size/addrs` | `itable` | `ilock()` 首次装载；inode sleeplock 保护读取和修改 |
| `struct file.ref/off/ip` | file table | descriptor 层共享与 offset；由前一通信单元解释 |

`nlink` 不是 open count，`inode.ref` 也不是 `struct file.ref`。`sys_unlink()` 可以把最后一个
name 清除并令 `nlink` 变为 0；只要 open file 仍持有 inode reference，数据仍可读。
只有 `iput()` 观察到 `ref==1 && valid && nlink==0`，才在放弃最后一个内存 reference 前
调用 `itrunc()`、把 `type` 写成 0，并释放 inode slot。

两个 inode 上界也不可混淆：`kernel/param.h:NINODE=50` 是同时 active 的内存 `itable`
slot 上界，`iget()` 找不到空 slot 会 panic；`mkfs/mkfs.c:NINODES=200` 写入
`sb.ninodes`，是 image 中可分配的 on-disk inode 数，`ialloc()` 扫尽后返回 0。增加后者不会
扩大前者，反之也不会改变 image format。

### Data mapping、截断与失败副作用

`bmap()` 对 logical block 0..11 直接使用 `ip->addrs[bn]`；第 12 个 logical block 先分配
`ip->addrs[NDIRECT]` 指向的 indirect metadata block，再在其中分配 data block。
`writei()` 即使没有增长 `size` 也会执行 `iupdate()`，因为失败前的 `bmap()` 可能已经改变
`addrs[]`。这形成以下可检查不变量：

- `12305` 字节 round-trip 必须得到 13 个 data block，`direct0/direct11/indirect/indirect0`
  都非零且互异；read bytes 与 checksum 必须匹配输入。
- `link(old,new)` 成功令同一 inode 的 `nlink: 1 -> 2`；依次 unlink 两个 name 后为 0，
  但 open reference 仍可读 `hello`，最后 close 才回收 blocks/inode。
- 对仍由另一个 fd 引用的 regular inode 执行 `open(path, O_TRUNC)`，`itrunc()` 释放两个
  data block、清空 mapping 并把 `size` 置 0；两个 fd 的 raw `inum` 相等且 peer 看到 size 0，
  重写 3 字节后仍是同一 inode。
- `sys_open(O_CREATE)` 在 `filealloc()/fdalloc()` 前调用 `create()`；fd exhaustion 可以返回
  `-1`，但新 directory entry 和 size-0 inode 已存在。失败返回值不等于无副作用。
- `sys_link()` 先增加并写回 `nlink`；若后续 `dirlink()` 失败，`bad:` 分支必须把它减回，
  且 target name、block 数与 inode 数不变。
- 在已占满 12 个 direct block 时，让 indirect metadata allocation 成功而下一次 data
  `balloc()` 失败：`filewrite()` 返回 `-1`，`size` 保持 `12288`，但 inode 保留一个空的
  indirect block，block ledger 增加 1。清理时 `itrunc()` 必须回收它。

## 源码追踪计划

先按层建立事实图，再进入局部分支：

```sh
rg -n '^fs.img:|^mkfs/mkfs:' Makefile
rg -n '^#define NINODES|^main\(|^ialloc\(|^iappend\(|^balloc\(' mkfs/mkfs.c
rg -n '^#define NINODE' kernel/param.h
rg -n 'struct superblock|struct dinode|struct dirent|ROOTINO|BSIZE|NDIRECT|NINDIRECT|MAXFILE|IBLOCK|BBLOCK' kernel/fs.h
rg -n '^readsb\(|^fsinit\(|^ialloc\(|^iget\(|^ilock\(|^iput\(|^bmap\(|^itrunc\(|^readi\(|^writei\(|^dirlookup\(|^dirlink\(|^namex\(' kernel/fs.c
rg -n '^sys_link\(|^sys_unlink\(|^create\(|^sys_open\(' kernel/sysfile.c
rg -n '^filealloc\(|^fileclose\(|^fileread\(|^filewrite\(' kernel/file.c
rg -n '^main\(' user/{cat,ln,ls,mkdir,rm}.c
rg -n 'writebig\(|createdelete\(|unlinkread\(|linktest\(|bigfile\(|dirfile\(' user/usertests.c
```

`cat/ln/ls/mkdir/rm` 是 namespace/data syscall 的薄 user-side clients：用它们确认参数和
`struct stat` 展示边界，但实现所有权仍在上述 kernel path；不要把命令行循环当作 inode 或
allocation 机制本身。

先回答[文件系统问题链](../questions/filesystem.md)的 `FILESYS-01..04`，画出 image-to-runtime
骨架；再用 `FILESYS-05..13` 逐项复核 ownership、mapping 和失败路径；最后用
`FILESYS-14` 将六个实验场景收束到同一资源账本。

## 观察任务

先运行不启动 QEMU 的 publication/static 门：

```sh
python3 docs/xv6-tutorial/resources/filesystem/run-lab.py --self-test
python3 docs/xv6-tutorial/resources/filesystem/run-lab.py --static-only
python3 docs/xv6-tutorial/tools/validate.py
python3 docs/xv6-tutorial/tools/generate_navigation.py --check
```

在 worksheet 中记录两条 breadth-first trace。第一条从 `Makefile:fs.img` 到
`mkfs/mkfs.c:main`、block 1 superblock、root `struct dinode`、root `struct dirent` 和 bitmap。
第二条从 `sys_open()`、`create()/namex()` 到 `struct file`，再从 `filewrite()` 经
`writei()/bmap()/bread()` 到 direct/indirect block，并由 `fileread()/readi()` 返回。
每一步都记录 owner、锁、reference、`inum/nlink/size/addrs` 和 buffer/disk-block handoff；
不要把调用了 `log_write()` 写成已经 durable。

## 有界修改任务

[`filesystem-audit.patch`](../resources/filesystem/filesystem-audit.patch) 只应用于 pinned
baseline 的临时导出。它加入 `_fstrace` 和 test-only `fsaudit` seam，读取 allocation bitmap、
on-disk inode 数、`itable` refs、file refs、buffer refs 与指定 inode mapping，并提供精确一次的
`dirlink`/`balloc` fault gate。它不是 kernel 修复或学习者答案，也不得留在共享 checkout。

`fstrace` 在一个私有 `fs.img` 上按固定 generation 执行六个场景：

1. `RW`：创建两段 path，写读 `12305` 字节并越过 direct/indirect 边界；
2. `LINK`：link、依次 unlink 两个 name，并通过仍打开的 fd 读取 `hello`；
3. `TRUNC`：写两个 block，以 `O_TRUNC` 清空，让 peer fd 观察 size 0 后重写 3 字节；
4. `FDFAIL`：占满可用 fd 后 `open(O_CREATE)` 失败，但复核遗留的 size-0 inode；
5. `LINKFAIL`：在 `nlink++` 后确定性拒绝 `dirlink()`，复核 rollback；
6. `ALLOCFAIL`：第 13 个 data block 的第二次 eligible `balloc()` 精确失败，复核空 indirect
   block 的部分副作用。

runner 只接受固定顺序的 `FS BASE/RW/AFTER/LINK/AFTER/TRUNC/AFTER/FDFAIL/AFTER/LINKFAIL/AFTER/ALLOCFAIL/AFTER/PASS`
marker。字段集合是闭集；缺失、重复、未知字段、非整数、顺序变化或额外 PASS 都失败。host
逐字段重算 byte/count/address/sequence 关系和每阶段 ledger，guest 自报 `status=0` 不是 oracle。

## Oracle、证据、失败路径和局限

| 维度 | 确定性触发器 | 必须观察到 | 不能推出 |
|---|---|---|---|
| S | pinned source anchors、image layout 与 patch scope | `mkfs -> superblock/root/bitmap`，`namex -> dirlookup -> inode`，`file -> readi/writei -> bmap -> buf/block` | buffer-cache eviction、log commit 或 crash atomicity |
| F | `RW`、`LINK`、`TRUNC` | exact bytes/checksum，13 data blocks，link/open-ref 生命周期，same-inum truncate/rewrite | 任意 workload 或跨 crash durability |
| B | `FDFAIL`、`LINKFAIL`、`ALLOCFAIL` 的命名 fault gate | create 遗留 inode、link rollback、空 indirect block 部分副作用，以及逐场景 cleanup | 所有 exhaustion 点或任意 fault timing |
| C | N/A | 本单元只使用确定性事件顺序，不提出并发正确性结论 | pathname/cache 的所有交错、scalability 或 fairness |
| R | N/A | 私有 image 只用于隔离副作用并在结束时删除 | log commit、host flush、tear、recovery 或 idempotence |

`FS BASE` 和每个 `FS AFTER phase=...` 都包含 `blocks/inodes/itable_active/itable_refs/file_objects/file_refs/buf_refs`。
每个 AFTER 必须逐字段回到 BASE，且 `buf_refs==0`；`LINK` 场景在 open reference 存活时可暂时
高于 BASE，但 close 后必须回零。允许副作用只有临时导出、build artifacts、私有 image、
短寿命 QEMU/process group 和显式 `/tmp` 报告。runner 最后 reverse patch、`make clean`、删除
私有 image，并验证共享 worktree/index 与共享 `fs.img` digest 不变；timeout 只作 watchdog。

## 退出产物与后续单元

按[报告模板](../resources/filesystem/report-template.md)提交一个报告包，并用
[rubric](../resources/filesystem/rubric.md)复核。报告必须内嵌 source/runtime worksheet、完整 raw
marker、host 重算、六个 AFTER ledger、focused/quick/full regression、patch/runner digest、
cleanup 与 `C/R=N/A` 局限。runner 的 `--report` 输出是机器证据附录；报告记录其 digest，
不能拿附录替代解释。

本页与[问题](../questions/filesystem.md)、[答案](../questions/answers/filesystem.md)已由
[0.1.0 非作者走查](../reviews/filesystem-0.1.0.md)复核并晋级 `verified`。后续
`core.persistence` 从本单元的 buffer boundary 接手 one-buffer-per-block、eviction、log
admission、commit、install、clear 和 recovery，并保留这里已经建立的
namespace/inode/data ownership。
