# 文件系统命名、inode 与数据路径答案与证据标准

## FILESYS-01

`Makefile` 用 `mkfs/mkfs` 把初始文件写入 `fs.img`；superblock 描述各 on-disk 区域。
directory data 中的 `struct dirent` 把固定长度 name 映射到 inode number，`struct dinode`
保存 type、`nlink`、size 与 block addresses。kernel 以 `(dev, inum)` 在 `itable` 中取得唯一的
活动 `struct inode` identity，file object 持有 inode reference 和 offset，descriptor 持有
file reference。`bread()` 取得某 physical block 的 buffer；buffer cache 和设备边界不改变
directory name、open reference 与 bitmap allocation 是三个不同 ownership 事实。

## FILESYS-02

`fs.img` 规则依赖 `mkfs/mkfs`、`README` 与 `UPROGS`。`mkfs:main` 清零整个 image，在 block 1
写 `struct superblock`，以 host-side `ialloc()` 建立 root 和 program inodes，以 `iappend()`
写 `.`、`..`、program bytes，最后写 allocation bitmap。`fsinit()` 经 `readsb()` 读取 block 1
并检查 `FSMAGIC`。`mkfs` 使用 `freeinode/freeblock` 的顺序分配和 `xshort/xint` 格式转换；kernel
的 `ialloc()/balloc()` 扫描 inode blocks/bitmap 并经 buffer/log 边界更新，因此两条路径只共享
format，不能互相替代运行时证据。

## FILESYS-03

`sys_open()` 进入 operation 外框后调用 `create()`。`nameiparent()` 返回 parent inode reference
和最后一个 name；`create()` 锁 parent，若 name 不存在则 `ialloc()` 新 inode、锁 inode、设置
metadata 并 `dirlink()`。成功返回的是仍锁定且被引用的 inode。之后 `filealloc()` 建立 file
reference，`fdalloc()` 把它放入当前进程 descriptor table，file 保存 `ip/off/readable/writable`。
关键 owner 顺序是 cwd/root reference -> locked parent -> new locked inode -> file object -> fd slot；
每个失败分支必须按其已取得的层次 cleanup。

## FILESYS-04

`sys_write()` 找到 file；`filewrite()` 为每个 log-sized chunk 包围 `begin_op/end_op` 并锁 inode；
`writei()` 按 `off/BSIZE` 调 `bmap()`，`bread()` physical block，copy bytes，调用 `log_write()`，
最后更新 inode size/mapping。成功 bytes 推进 file offset。`12305=12*1024+17`，所以 logical
blocks 0..11 使用 direct entries，logical block 12 经一个 indirect metadata block 到第一个
indirect data block，共 13 个 data block。read path 在 inode lock 下由 `readi()` 反向映射并
copyout，成功 bytes 推进 offset。这里能证明层间映射与 logical order，不能证明 log 已 commit
或 bytes 已 durable。

## FILESYS-05

absolute path 以 `iget(ROOTDEV, ROOTINO)` 取得 reference，relative path 以 `idup(cwd)` 取得。
每轮 `skipelem()` 后锁当前 inode；不是 directory 或找不到 name 时，`iunlockput()` 释放 lock 和
reference。`dirlookup()` 找到 `struct dirent` 后以 `iget(dp->dev, inum)` 返回 next reference；
循环再 `iunlockput()` current，把 ownership 转给 next。`nameiparent` 遇最后一段时只 unlock
current 并返回其 reference；caller 必须最终 `iput()`。空 path 且要求 parent 时，`namex()`
也会 `iput()` 后失败。

## FILESYS-06

`struct dinode` 是共享的 on-disk format；`type==0` 表示磁盘 inode free，`nlink` 只计 directory
links。`struct inode` 是内存 identity/cache：`ref==0` 表示 itable slot 可回收，`valid==0` 表示
cached metadata 尚未从 disk 装载。`itable.lock` 保护 `ref/dev/inum` 与 slot selection；inode
sleeplock 保护 `valid/type/nlink/size/addrs`。`iget()` 只取得 identity/reference，不读盘也不锁；
`ilock()` 才装载并验证 type。`iput()` 仅在 `ref==1 && valid && nlink==0` 时截断并把 type 写 0，
然后减少最后一个 ref。这些状态服务不同的不变量，不能合并。

`NINODE=50` 是内存 `itable` 的 active slot 上界；`iget()` 扫描不到 `ref==0` 的 slot 时 panic，
因为调用链没有可恢复返回值。`NINODES=200` 是 `mkfs` 写入 superblock 的 on-disk inode 数；
runtime `ialloc()` 扫到 `sb.ninodes` 仍无 `type==0` entry 时打印诊断并返回 0，创建路径再返回
`-1`。前者限制并发活动 identity，后者限制 image 中已分配对象，二者既不共享计数，也不共享
失败语义。

## FILESYS-07

`NDIRECT=12`，每个 block `BSIZE=1024`；`NINDIRECT=256`，所以 `MAXFILE=268` 个 data blocks。
logical block 12 先把 `bn` 减去 12；若 `ip->addrs[12]` 为 0，第一次 `balloc()` 分配 indirect
metadata block。`bread()` 该 block 后，若 `a[0]` 为 0，第二次 `balloc()` 分配 data block并
写入 entry。第 12305 个 byte（以长度计的最后 17 bytes）位于 logical block 12，因此文件有
13 个 data blocks，另有 1 个不计入 data-block 数的 indirect metadata block。

## FILESYS-08

`readi()` 把超出 EOF 的请求截短；copyout failure 返回 `-1`，而找不到 mapping 时返回已完成的
`tot`。`writei()` 拒绝 overflow/超过 `MAXFILE`；中途 `bmap()==0` 或 copyin failure 时停止，更新
已经到达的 size，并总是 `iupdate()`，因为 mapping 可能已改变。它返回实际 `tot`。
`filewrite()` 又把大请求拆成多个 transaction-sized chunk；只要某一 chunk 的 `r!=n1`，循环
停止并最终返回 `-1`，即使先前 chunk 已经写入、推进 offset 并完成 `end_op()`。因此 `-1`
不能被解释为零副作用。

## FILESYS-09

`sys_link()` 对 non-directory inode 先令 `nlink++`，成功 `dirlink()` 后两个 names 映射同一
inum。每次 `sys_unlink()` 先清对应 `struct dirent`，再令 inode `nlink--`。当序列变为
`1 -> 2 -> 1 -> 0` 时，open file 仍通过 `f->ip` 持有 inode reference，pathname lookup 已失败
但 `read(fd,...)` 仍可得到 `hello`。最后 `fileclose()` 在 file ref 到 0 时调用 `iput()`；只有
此时满足 last inode ref 与 zero links，`itrunc()` 才释放 data/indirect blocks并把 inode type
写 0。验收需同时看到 open window 的 held ledger 与 close 后回到 BASE。

## FILESYS-10

`sys_open()` 先找到并锁 inode，也先成功取得 file object/fd；对 regular file 的 `O_TRUNC` 才
调用 `itrunc()`。它逐项 `bfree()` direct data，遍历并释放 indirect data 后释放 indirect
metadata block，清空 addresses、置 `size=0` 并 `iupdate()`。另一个 file object 仍指向同一个
`struct inode`，所以 `fstat` 看到相同 inum 和 size 0；它自己的 file offset 不因 trunc 自动
重置。通过新打开 fd 重写 3 bytes 后 inode identity 不变，size/mapping 与相应 offsets 改变。

## FILESYS-11

`sys_open(O_CREATE)` 在 `filealloc()/fdalloc()` 之前完成 `create()` 与 parent `dirlink()`。
若 fd table 已满，`fdalloc()` 返回 `-1`；若 `filealloc()` 已成功，失败分支 `fileclose(f)` 归还
file object，随后 `iunlockput(ip)` 归还本次 inode reference。它没有 unlink 已创建的 name，
所以 syscall 返回 `-1` 而 path 存在、`nlink==1`、size 0、allocated inode 比 BASE 多 1。
显式 unlink 后，所有 file/inode refs 与 allocation ledger 才应回到 BASE。

## FILESYS-12

`sys_link()` 为避免新 directory entry 指向一个同时被释放的 inode，先在 inode lock 下增加
`nlink` 并 `iupdate()`。若 `nameiparent()`、跨 device 检查或 `dirlink()` 随后失败，`bad:` 再锁
inode、执行 `nlink--/iupdate/iunlockput`。确定性 `dirlink` fault 后应看到 return `-1`、target
lookup 失败、`nlink_before==nlink_after==1`，且 block/inode counts 不变。只看 `-1` 无法区分
正确 rollback、遗留 link count 或残留 directory entry。

## FILESYS-13

初始 size `12288` 已占 12 个 direct data blocks，`ip->addrs[NDIRECT]==0`。fault gate 让
`bmap()` 的第一次 eligible allocation 建立 indirect metadata block，让第二次 data allocation
返回 0；indirect entry 因此仍为 0。`writei()` 未复制 byte，`tot==0`、size 仍为 `12288`，但
末尾 `iupdate()` 把新的 indirect pointer 写回 inode。`filewrite()` 看到 short chunk 后返回
`-1`。raw marker 必须给出 `eligible=2/fired=1`、`blocks_before/blocks_after` 与
`inodes_before/inodes_after`；host 由此复算 `block_delta=1/inode_delta=0`。随后 close/unlink 的
`itrunc()` 必须释放这一个空 indirect block并回到 BASE。

## FILESYS-14

一个可信报告按固定顺序保留 `BASE`，每个 `RW/LINK/TRUNC/FDFAIL/LINKFAIL/ALLOCFAIL` 后紧跟
匹配 phase 的 `AFTER`，最后恰好一个 `PASS cases=6 cleanup=1`。host 拒绝未知、缺失、重复和
非整数字段，并自行检查 bytes/checksum、互异 block addresses、event sequence、link counts、
same inum、failure delta 与每个 AFTER 的七项 ledger 等于 BASE。`S` 支持源码/layout/ownership
关系，`F` 支持 normal round-trip/link/truncate，`B` 支持三个命名 failure。实验没有并发 claim，
私有 image 只隔离副作用，因此 `C=N/A`、`R=N/A`；QEMU timeout 和 guest PASS 都不能单独充当
正确性 oracle。
