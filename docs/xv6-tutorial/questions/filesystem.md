# 文件系统命名、inode 与数据路径问题

学习目标：先建立 image、namespace、inode、file 与 data block 的全局关系，再沿一条 read/write
路径进入局部实现，最后用 link/unlink/truncate 和精确 fault 复核 ownership。本问题链属于
已通过[非作者走查](../reviews/filesystem-0.1.0.md)的 `core.filesystem`；旧问题入口仅保留指向
本问题链的兼容说明。

## 高层总览

### FILESYS-01 一个 pathname 如何最终拥有一组 disk blocks？

从 `Makefile:fs.img`、`mkfs/mkfs.c:main`、`kernel/fs.h:struct superblock` 和
`kernel/sysfile.c:sys_open` 开始，画出 image、directory entry、inode、file object、buffer 与
disk block 的对象图。分别标出 name、open reference 和 allocated block 由谁拥有。

## 流程骨架

### FILESYS-02 初始 image 如何成为 runtime filesystem？

从 `Makefile:fs.img` 追到 `mkfs/mkfs.c:main/ialloc/iappend/balloc`，再接到
`kernel/fs.c:fsinit/readsb`。按 block 区域列出 superblock 中的定位字段，并解释为什么 host
`mkfs:ialloc` 不能当作 kernel `fs.c:ialloc` 的 runtime 证据。

### FILESYS-03 `open("fsd/data", O_CREATE | O_RDWR)` 如何建立 namespace 与 open ownership？

breadth-first 追踪 `sys_open -> create -> nameiparent/namex -> dirlookup/dirlink -> ialloc`，再到
`filealloc/fdalloc`。先列阶段和每阶段返回的 owner，不要先深入每次 `bread()`。

### FILESYS-04 `write(fd, payload, 12305)` 再 `read` 如何跨越所有层？

从 `sys_write/filewrite/writei/bmap/bread` 到 direct/indirect blocks，再从
`sys_read/fileread/readi` 返回。记录 file offset、inode size、logical block、physical block、
buffer lock/ref 和 byte count；指出 `log_write()` 在本单元为什么只是边界。

## 局部深入

### FILESYS-05 `namex()` 如何在逐段 lookup 时转移 inode reference？

比较 absolute path 的 `iget(ROOTDEV, ROOTINO)`、relative path 的 `idup(cwd)`、循环中的
`ilock/dirlookup/iunlockput`，以及 `nameiparent` 最后一段前的 early return。每条失败边由谁
释放当前 reference？

### FILESYS-06 `struct dinode`、`struct inode` 与两个 inode 上界为什么不能合并？

从 `kernel/fs.h:struct dinode`、`kernel/file.h:struct inode` 和 `kernel/fs.c:iget/ilock/iput`
比较 `type/nlink/size/addrs`、`ref/valid` 和两把 lock 的职责。说明 on-disk free、itable slot
free、cached fields valid 与 locked 是四个不同状态；再比较 `kernel/param.h:NINODE` 与
`mkfs/mkfs.c:NINODES` 的耗尽行为，说明为什么修改其中一个不会扩大另一个。

### FILESYS-07 第 12305 个 byte 为什么需要两个新 block？

从 `BSIZE/NDIRECT/NINDIRECT/MAXFILE` 和 `bmap()` 计算 logical block 12 的路径。区分
`ip->addrs[NDIRECT]` 指向的 indirect metadata block 与其中 `a[0]` 指向的 data block，并给出
`12305` 字节文件应有的 data-block 数。

### FILESYS-08 `readi()` 与 `writei()` 在 partial result 上有什么不同边界？

比较 range check、`bmap()`、`either_copyin/either_copyout`、`ip->size`、`iupdate()` 和返回值；
再读 `filewrite()` 的 chunk loop，解释为何 syscall 最终可返回 `-1` 而此前 chunk 已经完成。

## 横切 ownership 与失败路径

### FILESYS-09 最后一个 pathname 消失后，打开的文件为什么仍可读？

沿 `sys_link()`、`sys_unlink()`、`fileclose()` 和 `iput()` 区分 directory entry、`nlink`、
`struct file.ref` 与 `inode.ref`。给出 `1 -> 2 -> 1 -> 0` 后仍读到 `hello`，以及真正回收
blocks/inode 的精确条件。

### FILESYS-10 `O_TRUNC` 如何让另一个 fd 看到同一 inode 的 size 0？

从 `sys_open()` 中 `filealloc/fdalloc` 后的 `itrunc()`，追踪 direct、indirect、size 与
`iupdate()`。说明 peer fd 的 identity 与 offset 如何区别，并判断重写 3 字节后哪些字段改变。

### FILESYS-11 为什么 `open(O_CREATE)` 返回 `-1` 后 path 仍可能存在？

让 per-process fd table 确定性耗尽，按 `create()`、`filealloc()`、`fdalloc()` 和失败 cleanup
的真实顺序解释结果。列出必须恢复的 file/inode refs，以及明确保留的 namespace 副作用。

### FILESYS-12 `sys_link()` 的 late `dirlink()` failure 如何回滚？

从 `nlink++/iupdate` 到 `bad:` 分支逐项记录 inode、target directory entry、block 和 inode
ledger。为什么只检查 syscall 返回 `-1` 不能证明 rollback 正确？

### FILESYS-13 indirect data allocation 失败后为何可能多出一个 block？

先写满 12 个 direct block，再令 `bmap()` 中第一次 eligible `balloc()` 成功、第二次精确失败。
结合 `writei()` 无 size 增长也调用 `iupdate()`，预测 return、size、indirect pointer、indirect
entry、block delta 与 cleanup。

## 全流程串联

### FILESYS-14 如何用一个报告包复核六个场景而不越过证据边界？

将 `FS BASE/RW/LINK/TRUNC/FDFAIL/LINKFAIL/ALLOCFAIL/AFTER/PASS` 的 closed schema 和顺序，
`blocks/inodes/itable_active/itable_refs/file_objects/file_refs/buf_refs` ledger、isolated image、
focused/quick/full regression 与 cleanup 连接起来。分别填写 `S/F/B` 结论，并说明为何
`C=N/A`、`R=N/A`。

答案与证据标准见[配套答案](answers/filesystem.md)。
