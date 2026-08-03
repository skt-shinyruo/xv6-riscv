# 固定资源、耗尽行为与回收矩阵

xv6 使用固定数组、固定页池和固定磁盘布局。不同层的“没有资源”并不具有统一语义：有的返回 `-1`，有的睡眠等待，有的杀死当前进程，有的直接 panic，还有些失败会保留已完成的副作用。本文把容量、分配点、耗尽行为、归还点和验证方法放到同一张账本中。

日志块的严格集合推导见[文件系统与日志资源上界](../kernel/resource-bounds.md)。本文覆盖该文刻意排除的进程、file、inode cache、设备队列和其他全局资源。错误分类的总体边界见[信任与失败模型](../architecture/trust-and-failure-model.md)。

## 1. 结果分类

| 结果 | 调用者看到什么 | 资源状态 |
|---|---|---|
| 返回 `-1` | 当前系统调用通常返回失败 | 可能完整回滚，也可能已有名字、偏移、数据或 lazy 页副作用 |
| 短计数/0 | read/write 或 helper 部分成功、EOF 或特定失败 | 已完成前缀通常保留 |
| kill 当前进程 | 下一次统一 killed 检查进入 `kexit(-1)` | 内核继续运行；资源由 exit/wait 路径回收 |
| 睡眠 | 调用暂不返回 | 必须存在 producer、条件锁和唤醒；不保证公平或超时 |
| panic | 整个内核停止正常执行 | 教学实现把该条件视为破坏内部前提或不可恢复耗尽 |
| 无限等待/活锁 | 无显式错误 | 可能来自遗漏 producer、日志 group 不归零或重复 level IRQ |

任何测试都必须明确期望哪一类结果。只检查“QEMU 没 panic”不能证明调用返回值、回收或持久状态正确。

## 2. 编译期容量总表

| 资源 | 当前容量 | 分配/认领入口 | 耗尽行为 | 主要归还点 |
|---|---:|---|---|---|
| hart/CPU 槽 | `NCPU=8` | `_entry` 以 hart id 索引 | 无检查；越界栈/CPU 状态访问 | 不归还 |
| 进程槽 | `NPROC=64` | `allocproc()` 线性扫描 | `kfork()` 返回 `-1`；首进程失败会 panic | `kwait()->freeproc()` 或未发布构造回滚 |
| 每进程 fd | `NOFILE=16` | `fdalloc()` 线性扫描 | `dup/open/pipe` 返回 `-1`，可能已有副作用 | `close()`、`kexit()` |
| 全局 file | `NFILE=100` | `filealloc()` 线性扫描 | 返回空，外层通常 `-1` | 最后 `fileclose()` 把 ref 降到 0 |
| 活跃 inode cache | `NINODE=50` | `iget()` | `panic("iget: no inodes")` | `iput()` 把 ref 降到 0；槽可复用 |
| 设备 major 槽 | `NDEV=10` | 静态 `devsw[]` | open 对越界 major 返回 `-1`；空回调到 I/O 才失败 | 静态，不归还 |
| exec argv 槽 | `MAXARG=32` | `sys_exec()` 内核数组 | 超出返回 `-1`；最多 31 个非空参数 | syscall 返回时释放参数页 |
| 内核路径缓冲 | `MAXPATH=128` | `argstr()`/各路径 syscall | 128 字节窗口内无 NUL 返回 `-1` | 栈对象自动释放 |
| 用户 exec 栈 | `USERSTACK=1` 页 | `kexec()` | argv/字符串越过 stackbase 返回 `-1` | exec 失败回滚或进程页表释放 |
| buffer cache | `NBUF=30` | `bget()` | 无 `refcnt==0` victim 时 panic | `brelse()`/`bunpin()` 降引用 |
| 单 operation 日志预算 | `MAXOPBLOCKS=10` | `begin_op()` 名义预留 | 实际超预算可使 `log_write()` panic | `end_op()` 后 group commit |
| 内存/磁盘日志项 | `LOGBLOCKS=30` | `log_write()` | admission 睡眠；满后调用甚至 absorption 前 panic | commit 后清 `lh.n`、unpin |
| 文件系统块 | `FSSIZE=2000` | `balloc()` | 扫完 bitmap 返回 0；上层可能短写或 `-1` | `bfree()`/`itrunc()` |
| 单文件数据块 | `MAXFILE=268` | `bmap()/writei()` | 越界检查返回失败；部分旧分片可已提交 | truncate/unlink 最后引用 |
| 磁盘 dinode | `NINODES=200` 个表项；可分配 inum 为 `[1,200)`，最多 199 个 | `ialloc()` | 无空 dinode 返回 0，外层通常 `-1` | 最后 `iput()` 或启动 orphan reclaim |
| pipe 数据 | `PIPESIZE=512` 字节 | `pipewrite()` | 满时睡眠；读端关闭/killed 返回 `-1` | reader 消费；两端关闭后释放页 |
| console 输入 | 128 字节 | UART RX/console buffer | 满时发布；reader 未释放空间前新输入被丢弃 | `consoleread()` 推进 `r` |
| VirtIO descriptor | `NUM=8` | `alloc3_desc()` | 少于 3 个时睡眠 | 请求者在完成后 `free_chain()` |
| VirtIO in-flight 请求 | 最多 2 | 每请求 3 descriptors | 第三笔等待 descriptor | 完成 IRQ 唤醒请求者后释放 |
| shell argv | `MAXARGS=10` | parser | 第 10 个非空项触发 shell panic | 命令进程退出 |

容量相同不代表资源相同。`ROOTDEV=1`、console major 1、VirtIO IRQ 1 和 queue 0 分别属于设备号、文件 major、PLIC source 和 VirtIO queue 编号空间，数值相同没有共享身份。

## 3. hart 与启动资源

### 3.1 `NCPU`

`_entry` 在任何 C 检查前用 `mhartid` 选择 `stack0[(id+1)*PGSIZE]`，随后 `cpuid()` 直接用 `tp` 索引 `cpus[]`。因此 `CPUS>NCPU` 或稀疏/过大 hart id 不是可恢复配置错误，可能先破坏静态内存再表现为任意 panic。

验证必须同时检查 QEMU `-smp` 和内核 `NCPU`。单纯成功编译不能证明启动参数安全。

### 3.2 早期与 scheduler 栈

`stack0` 为每 hart 提供一页连续栈，没有 guard page。它既用于启动，也用于 scheduler/idle trap context。进程内核栈另由 `proc_mapstacks()` 预先为每个进程槽分配一页并设置虚拟 guard；两类栈不能合并记账。

`proc_mapstacks()` 分配 `NPROC` 页失败会在启动期 panic，没有降级到更少进程槽的协议。

## 4. 进程与物理内存

### 4.1 `NPROC`

`allocproc()` 逐槽取锁查找 `UNUSED`。找到槽后还可能在 pid、trapframe 或页表分配阶段失败；这些失败清理未发布槽并返回 0。`kfork()` 最终返回 `-1`，但已经递增的全局 pid 不回退，所以资源耗尽允许 pid 跳号。

`forktest` 验证进程表耗尽后已创建 child 能被回收，并能再次 fork。它不精确区分“进程槽耗尽”和“物理页不足”，因为每个 child 还需要 trapframe、页表和用户页副本。

### 4.2 物理页

物理页池是 `[PGROUNDUP(end), PHYSTOP)`。没有独立容量常量；静态内核增大、`PHYSTOP`、QEMU RAM 和启动期永久页共同决定可用页数。主要消费者包括：

- `NPROC` 个永久进程内核栈；
- kernel page table 和用户多级页表；
- trapframe 与用户叶页；
- pipe 页面；
- VirtIO 三个 queue 页面；
- 临时 exec/fork 页表峰值。

普通 `kalloc()` 失败返回 0。上层结果不同：

| 路径 | 结果 |
|---|---|
| eager `sbrk` | 回滚本轮用户页，系统调用返回 `-1` |
| `fork` | 回滚未发布 child，父进程收到 `-1` |
| 用户硬件 lazy fault | `vmfault()` 失败，普通进程被 kill；`initproc` 最终进入 `kexit()` 时 panic |
| `copyin/copyout` lazy fallback | helper 返回失败；调用者决定 `-1`、短计数或其他语义 |
| `pipealloc` | 关闭已取得 file 并返回失败 |
| VirtIO queue 初始化 | 启动期 panic |
| kernel page table/stack 初始化 | 启动期 panic |

页表中间层可能在叶映射失败或缩容后保留空页，直到整个地址空间销毁。因而 `p->sz` 很小不等于该进程页表只占很少物理页。

## 5. fd、file 与 inode cache

### 5.1 每进程 fd 与全局 file

`fdalloc()` 只把调用者已经拥有的 file 指针装入最低空槽，不增加引用。`sys_pipe()` 同时需要两个 fd 和两个 file；任何阶段失败都必须清除已安装槽并关闭相应引用。

`sys_open(O_CREATE)` 的名字创建发生在 file/fd 分配之前。如果 `create()` 已把新空文件链接进目录，随后 `filealloc()` 或 `fdalloc()` 失败，open 返回 `-1`，但文件名可保留并随事务持久化。该路径是资源失败伴随用户可见副作用的典型反例。

`NFILE` 耗尽与 `NOFILE` 耗尽都只返回 `-1`，没有 `errno` 区分。测试若只检查返回值，必须额外观察全局 file ref 和当前 `ofile[]` 才能定位层次。

### 5.2 `NINODE` 与磁盘 `NINODES`

两者完全不同：

- `NINODE=50` 是内存 inode identity/ref cache。50 个不同 inode 同时保持引用后，下一次 `iget()` panic。
- `NINODES=200` 是 mkfs 生成的磁盘 dinode 表项数。inode 0 永久保留，`ialloc()` 只扫描 `[1,200)`，所以理论上最多 199 个可分配 inode；干净镜像已占 root 和预装文件，实际剩余更少。扫完没有空 type 时返回 0。

同一 inode 被多个 fd 打开只占一个 inode-cache 槽，但增加 ref。`usertests outofinodes` 每轮关闭 fd 后继续创建，主要压力是磁盘 dinode，不验证活跃 `NINODE` cache panic。

## 6. 路径、参数与用户栈

`MAXPATH` 是内核复制窗口，不是完整文件名语义。每个目录分量在磁盘上只有 `DIRSIZ=14` 字节；超长分量被截断比较，可能产生别名。shell `MAXARGS=10` 又比内核 `MAXARG=32` 更小，交互命令最多 9 个非空项。

exec 有三层容量：

1. `sys_exec()` 最多编组 `MAXARG-1=31` 个字符串，留下 NULL 槽。
2. 每个字符串复制到单独一页的内核临时 buffer，必须在页内找到 NUL。
3. 全部字符串和 argv 指针必须装入 `USERSTACK=1` 页，并保持 16 字节对齐、避开 guard。

任一提交前失败都保留旧映像，但从旧 lazy 地址空间读取参数时已物化的页不会回滚。

## 7. buffer cache、日志与磁盘

### 7.1 `NBUF`

`bget()` 需要 `refcnt==0` 的 victim。被普通 caller 持有、等待 I/O 或被日志 pin 的 buffer 都不可复用；没有 victim 时直接 panic，不睡眠。`NBUF==LOGBLOCKS` 不是安全证明，因为 commit 还要临时获取 log/header/home buffer，并发 reader 也可占槽。

合法并发 cache miss 可先各自占住 buffer，再因只有两个 VirtIO 请求槽而等待 descriptor。descriptor backpressure 不会自动归还这些 buffer，因此 cache、日志 pin 和设备队列必须联合分析。

### 7.2 日志准入与活性

`begin_op()` 在以下条件睡眠：正在 commit，或最坏预留会超过 `LOGBLOCKS`。producer 是 `end_op()` 释放预留或 commit 完成后对 `&log` 的唤醒。

活性依赖每个进入的 operation 最终调用一次 `end_op()`，且 `outstanding` 最终归零。持续重叠 operation 可以使已经返回的更新长期不提交；没有 epoch、最大等待时间、定时 commit 或公平准入。

`log_write()` 在 absorption 扫描前检查 `lh.n>=LOGBLOCKS`。错误预算若已经填满 30 项，下一次即使重复登记已有 block 也会 panic。

### 7.3 数据块和 dinode

`balloc()` 扫描 superblock 指定范围；没有空位返回 0。`bmap()` 将其转换为上层失败，`writei()` 可返回已完成前缀。`filewrite()` 若任何 chunk 短写，最终返回 `-1`，但此前完整 chunk、offset 推进和本 chunk 前缀仍可能存在。

`MAXFILE=NDIRECT+NINDIRECT=268` 个 1 KiB 数据块，即 274432 字节。没有 double-indirect、extent 或 sparse write。超过边界的请求不能假定整个 write 零副作用。

## 8. pipe、console 与设备队列

### 8.1 pipe

pipe 页面需要一物理页和两个 file 对象。构造失败按已取得顺序关闭回滚。运行期容量满时 writer 在 `&nwrite` 睡眠；reader 消费后在同一 pipe 锁下唤醒。没有读端或 writer killed 时返回 `-1`，已写前缀保留。

成功写入 `n>PIPESIZE` 必然至少睡眠一次，因为 writer 持锁时 reader 不能消费。多个 writer 只在满缓冲释放锁时产生交错，不存在独立 `PIPE_BUF` 保证。

### 8.2 console

console 使用累计 `r/w/e` 和 128 字节数组。达到容量会强制把编辑区发布为可读边界；如果 reader 尚未推进 `r`，之后字符会被丢弃。驱动不报告 overrun，因而输入丢失不是系统调用错误返回。

多个 reader 共享同一个全局输入流。一个进程触发的延迟 `Ctrl-D` 可由下一次取得 console 锁的另一个 reader 消费；没有终端会话或前台进程组隔离。

### 8.3 VirtIO

queue `NUM=8`，每个 block 请求固定占三个 descriptor，所以最多两笔在途。第三笔在 `&disk.free[0]` 睡眠；完成 IRQ 只清 `b->disk` 并唤醒原请求者，descriptor 由请求者醒来后释放。

设备返回非零 status、初始化不匹配或部分内部一致性检查失败会 panic，不转换为用户 `EIO`。实现也没有 timeout、reset/retry 或设备热拔出恢复。

## 9. 资源归还检查表

| 操作 | 必须归还/保留 |
|---|---|
| fork 构造失败 | child trapframe、页表、用户页、槽；pid 可跳号 |
| exec 提交前失败 | 新页表和参数页；旧映像、fd、cwd 保留；旧 lazy 参数页可已物化 |
| exec 成功 | 旧用户页和页表释放；fd/cwd 保留 |
| exit | 关闭 fd/cwd、reparent；保留 proc 槽、trapframe、页表到 wait |
| wait 坏 status 地址 | 返回 `-1`，zombie 必须仍可再次 wait 回收 |
| pipe 构造失败 | 两个 file、pipe 页、已安装 fd 槽全部回滚 |
| open 创建后 fd/file 失败 | inode ref 回收；新目录项可能保留 |
| write 短写 | 完成前缀、offset 和日志登记按实际路径保留 |
| log commit | 清 outstanding/group、unpin home buffer；磁盘 header 清零 |
| VirtIO completion | 唤醒请求者；请求者释放 descriptor chain 和 in-flight 账本 |

## 10. 验证矩阵

| 资源/行为 | 现有入口 | 仍需补充的强 oracle |
|---|---|---|
| `NPROC` | `forktest`、`usertests forkfork/forkforkfork` | 区分槽耗尽与 OOM，验证 pid 跳号后仍唯一 |
| 物理页 | `sbrkfail`、`execout`、每轮 `countfree()` | 精确故障点和中间页表页账本 |
| `NOFILE` | 无直接容量测试；常规 fd tests 只间接使用表 | 精确填满 16 槽，验证失败、释放一槽和再次分配 |
| `NFILE` | 无直接容量测试；现有并发文件测试未填满全局表 | 多 holder 独立 open，耗尽全局表而不先撞每进程 fd |
| `NINODE` cache | 无直接测试 | 同时 pin 50 个不同 inode，明确预期当前实现 panic |
| 磁盘 dinode | `outofinodes` | 断言确实耗尽、清理后可再次创建 |
| `NBUF` | 间接由并发文件测试覆盖 | 控制 pin/reader/commit 交错，明确 panic 或 headroom |
| 日志空间 | `logstress`、`manywrites` | 每 operation 实际 unique blocks 和等待公平性 |
| 磁盘块 | `diskfull` | 清理后重新写满，证明 block 全部恢复 |
| pipe | `pipe1`、`sharedfd` 及 pipeline 路径 | 多 writer、broken-pipe 部分写、零长度空读 |
| VirtIO descriptors | 并发 I/O 压力 | 观测最多两笔在途、第三笔等待及无泄漏 |
| console | 交互测试 | 满缓冲丢字符、多 reader/`Ctrl-D` 归属 |

具体注入方法和验收记录格式见[故障注入](../verification/fault-injection.md)。测试名到证明范围的反向索引见[源码到测试追踪矩阵](source-test-traceability.md)。

## 11. 修改容量时的联合审查

1. 提高 `NPROC` 会同时增加永久 kernel stack 页、scheduler 扫描成本、wakeup 中断延迟和最大 fd 引用数量。
2. 提高 `NOFILE` 但不提高 `NFILE/NINODE` 只会把耗尽移动到下一层。
3. 提高 `FSSIZE` 可能增加 bitmap blocks，使 truncate/unlink 超过 `MAXOPBLOCKS`。
4. 改 `MAXOPBLOCKS` 会连带改变 `LOGBLOCKS/NBUF`、镜像布局和 filewrite chunk 公式。
5. 提高 VirtIO `NUM` 需要重新核对共享结构大小、ring 回绕、descriptor 账本和 buffer cache headroom。
6. 提高 `USERSTACK/MAXARG` 需要同步核对 exec 栈布局、内核临时页峰值和测试边界。
7. 调整 QEMU RAM 必须同步 `PHYSTOP`；更大宿主 RAM不会自动被内核使用，更小 RAM 会让 allocator 管理不存在的地址。

固定容量是 xv6 简化设计的一部分，但失败行为仍是用户和内核的可观察契约。每次扩容都应同时回答“谁先耗尽、如何等待、何时归还、能否再次成功”，而不是只修改数组长度。
