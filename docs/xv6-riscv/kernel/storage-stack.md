# Buffer Cache、日志与 VirtIO 存储栈

本文沿一个磁盘 block 从内存缓存到 redo log，再到 QEMU VirtIO block device 的路径说明当前实现。三个层次分别解决不同问题：buffer cache 提供 block 身份唯一性和同步点，日志提供多 block 更新的崩溃原子性，VirtIO 驱动负责 DMA 请求提交和中断完成。

## 1. 源码地图

| 源码 | 责任 |
|---|---|
| [`kernel/buf.h`](../../../kernel/buf.h) | `struct buf` 的缓存、锁、引用和设备所有权字段 |
| [`kernel/bio.c`](../../../kernel/bio.c) | 固定大小 buffer cache、查找/替换、读写、pin/unpin |
| [`kernel/log.c`](../../../kernel/log.c) | 物理 redo log、空间预留、批量提交和启动恢复 |
| [`kernel/virtio.h`](../../../kernel/virtio.h) | VirtIO MMIO 寄存器、descriptor、avail/used ring 格式 |
| [`kernel/virtio_disk.c`](../../../kernel/virtio_disk.c) | 设备协商、请求 descriptor 链、通知、睡眠和完成中断 |

关联入口包括 `kernel/fs.c` 的 `bread()/log_write()` 调用、`kernel/file.c:filewrite()` 的事务分批、`kernel/trap.c:devintr()` 的中断分派，以及 `kernel/param.h` 中相互耦合的 `MAXOPBLOCKS/LOGBLOCKS/NBUF`。PLIC 的 priority、claim/complete 与每 hart 路由见[设备文档](devices.md)。

## 2. 分层调用关系

正常文件数据读取：

```text
readi
  -> bread
      -> bget
      -> [cache miss] virtio_disk_rw(read)
  -> either_copyout                 # user_dst 决定 copyout 或 memmove
  -> brelse
```

文件系统更新不会直接把 home block 写回磁盘：

```text
begin_op
  -> bread + modify b->data
  -> log_write(b)              # 只登记并 pin，尚未落盘
  -> brelse
end_op(last outstanding interval)
  -> write_log                 # cache -> on-disk log data blocks
  -> write_head                # 持久化日志头，真正 commit point
  -> install_trans             # log blocks -> home blocks
  -> clear log header
```

所有实际 block I/O 最终都进入 `virtio_disk_rw()`；日志只改变写入顺序和目标 block，不绕过设备驱动。

## 3. `struct buf` 的状态与所有权

`kernel/buf.h` 中每个 buffer 包含：

| 字段 | 含义 | 保护方式 |
|---|---|---|
| `dev/blockno` | 该槽当前代表的唯一磁盘 block | `bcache.lock` |
| `refcnt` | 正在使用、等待或被日志 pin 的引用数 | `bcache.lock` |
| `prev/next` | 全局近似 LRU 双向链表 | `bcache.lock` |
| `valid` | `data` 是否已经包含磁盘内容 | buffer sleeplock |
| `data[BSIZE]` | 1 KiB block 内容 | buffer sleeplock |
| `disk` | VirtIO 是否正在使用该 buffer | `disk.vdisk_lock` |
| `lock` | 单个 block 的长临界区 | sleeplock 自身内部锁 |

`refcnt` 不是磁盘引用计数，也不表示内容是否脏。它只决定该缓存槽能否被重新指定给另一个 `(dev, blockno)`。日志通过额外增加 `refcnt` 防止尚未提交的修改被替换。

## 4. Buffer cache 初始化和身份唯一性

`binit()` 初始化 `NBUF` 个固定槽，并把它们连成以 `bcache.head` 为哨兵的双向链表。链表头侧是最近释放、尾侧是最久未使用。

`bget(dev, blockno)` 在持有 `bcache.lock` 时完成两阶段查找：

1. 从头到尾查找相同 `(dev, blockno)`；命中后增加 `refcnt`；
2. 未命中时从尾到头找 `refcnt == 0` 的槽，重新填写身份、清 `valid`、设 `refcnt=1`。

身份查找与槽重新指定都在同一把 spinlock 下，所以不会同时出现两个代表同一 block 的 buffer。释放全局锁后才获取该 buffer 的 sleeplock；如果另一个进程正在使用同一 block，调用者可以睡眠而不阻塞整个 cache。

找不到 `refcnt==0` 的槽会 `panic("bget: no buffers")`，当前实现没有动态扩容或等待空闲 buffer 的机制。因此日志预算必须保证 pin 不会占满整个 cache。

## 5. `bread/bwrite/brelse`

### 5.1 `bread()`

`bread()` 返回一个已持有 sleeplock 的 buffer。若 `valid==0`，同步调用 `virtio_disk_rw(b, 0)`；请求完成后把 `valid` 置一。多个读者请求同一 block 时，只有第一个执行磁盘读，后续读者在 sleeplock 上等待并复用内容。

### 5.2 `bwrite()`

调用者必须持有 buffer sleeplock。`bwrite()` 直接同步提交 VirtIO 写请求，返回时设备已经报告成功；它不自动登记日志。

普通文件系统更新必须调用 `log_write()` 而不是直接 `bwrite()`。直接写 home block 会破坏多 block 更新的崩溃原子性。`log.c` 自己在写日志块、日志头和安装 home block 时使用 `bwrite()`，因为这些步骤正是日志协议本身。

### 5.3 `brelse()`

先释放 buffer sleeplock，再在 `bcache.lock` 下减少 `refcnt`。降到零时将槽移到链表头，表示最近使用。只有最后一个引用释放时才移动，所以这是近似 LRU，而不是每次访问都更新的精确 LRU。

`brelse()` 后调用者不得继续访问 `b->data`；槽可能立即被另一个 block 复用。

## 6. Pin 与日志吸收

`bpin()`/`bunpin()` 只在 `bcache.lock` 下增减 `refcnt`，不获取 buffer sleeplock，也不检查加减是否配对。`refcnt` 是无符号数；错误的重复 `bunpin()` 会下溢为很大的值，使该槽永久不可回收，而不是立即 panic。

`log_write(b)` 在 `log.lock` 下检查当前内存日志头：

- 若 `b->blockno` 尚未出现，将 block number 追加到 `log.lh.block[]`、调用 `bpin()` 并增加 `lh.n`；
- 若已经出现，只保留一项，这称为 log absorption；同一批事务多次修改同一 block 只占一个日志槽，最终提交最新 cache 内容。

调用 `log_write()` 时 buffer 应由调用者锁定且内容已修改；这是调用约定，函数没有像 `bwrite()` 那样用 `holdingsleep()` 验证。函数本身不会复制数据，真正复制发生在最后一个文件系统操作结束后的 `write_log()`。登记键只有 `blockno`，没有 `dev`；这与本仓库只挂载一个文件系统设备的假设绑定，不能直接扩展为多设备日志。

还有一个容易忽略的边界：`log_write()` 在做 absorption 扫描**之前**先检查 `lh.n >= LOGBLOCKS`。因此若错误的预算已让内存日志达到满容量，即使下一次登记的是已有 block，也会先触发 `panic("too big a transaction")`。

## 7. 日志的内存态和磁盘态

磁盘镜像通过 superblock 的 `logstart/nlog` 描述日志区域；当前 `mkfs/mkfs.c` 固定生成 `nlog = LOGBLOCKS + 1`，即一个 header 加 `LOGBLOCKS` 个 redo data blocks：

```text
log.start + 0       log header: n, home block numbers[]
log.start + 1       第 0 个 redo 数据 block
...
log.start + n       第 n-1 个 redo 数据 block
```

磁盘和内存共用的 `struct logheader` 含 `int n` 与 `int block[LOGBLOCKS]`；清日志只把 `n` 写成零，旧 block number 字节仍可残留但不再有效。`initlog()` 只检查整个 header 严格小于 `BSIZE`，运行时既不保存 `sb->nlog`，也不验证它是否至少为 `LOGBLOCKS + 1`。因此镜像布局、`kernel/param.h` 和内核二进制必须匹配。

内存中的 `struct log` 还维护：

- `outstanding`：已经进入 `begin_op()`、但尚未执行配对 `end_op()` 的日志操作区间数；
- `committing`：当前是否由最后一个操作执行 commit；
- `lh`：这一批已修改的不同 home blocks；
- `dev/start`：日志所在设备和起始 block。

这里的“操作区间”不是“系统调用”的别名。一次大 `write()` 会建立多个区间；最后一个 inode/device file 引用的 `fileclose()`、`kexit()` 释放 cwd、`kexec()` 和启动期 `ireclaim()` 等内核路径也会建立区间。反过来，pipe I/O、设备 I/O 和许多只读路径不经过日志；pipe close 和非最后 file 引用的 close 也不建区间。多个区间可以并发加入同一 group transaction，但只在 `outstanding` 降到零时整体提交。日志不保存各区间的子事务边界，因为 commit 永远不会在任一参与区间尚未结束时发生。

这也意味着“系统调用已经返回”不等于“其修改已经单独持久化”：一个区间先执行完 `end_op()` 时，若仍有其他 outstanding 区间，它的上层路径可以继续乃至返回用户态，而提交继续延后。此时崩溃可能丢失这个已经返回的操作，但日志仍保证磁盘结构落在整批提交之前或之后的一致状态。这里提供的是 group 的崩溃原子性，不是 `fsync` 式的逐调用持久性；本仓库也没有 `fsync` 系统调用。

## 8. `begin_op()` 的空间预留

调用者进入可能修改文件系统的日志操作区间前必须调用 `begin_op()`。它在 `log.lock` 下循环检查：

1. 若 `committing` 为真，睡眠等待提交结束；
2. 若 `lh.n + (outstanding + 1) * MAXOPBLOCKS > LOGBLOCKS`，为最坏情况空间不足，睡眠等待现有批次提交或预留释放；
3. 否则增加 `outstanding` 并返回。

这个公式把每个在途区间都按最多修改 `MAXOPBLOCKS` 个不同 block 预留，即使实际尚未调用 `log_write()`。只有在每个区间确实不超过该上限、且 `begin_op()/end_op()` 严格配对时，它才能保证并发参与者不会共同把日志挤爆；代码不记录每个区间的实际配额，也不防御多调一次 `end_op()` 把有符号的 `outstanding` 减成负数。

等待通道是 `&log`，条件锁是 `log.lock`。睡醒后必须重新检查 `committing` 和容量，不能假定资源一定属于当前进程。

## 9. `end_op()` 和 group commit

`end_op()` 减少 `outstanding`：

- 仍有其他区间时，唤醒可能因空间预留而等待的 `begin_op()`；
- 降到零时，当前进程设置 `committing=1` 并成为本批提交者。

提交者在释放 `log.lock` 后调用 `commit()`，因为内部磁盘 I/O 会睡眠。完成后重新获取锁、清 `committing` 并唤醒所有等待者。

由此得到两个重要不变量：

- `commit()` 运行期间没有新的日志操作区间进入当前批次；
- `commit()` 不持有 `log.lock` 做磁盘 I/O，但 `committing` 阻止其他调用者修改 `lh`。

## 10. 四阶段提交与崩溃语义

当 `lh.n > 0` 时，`commit()` 执行：

1. `write_log()`：把每个修改后的 cache block 写入对应 redo 数据 block；
2. `write_head()`：把 `n` 和 home block number 列表写入 header；
3. `install_trans(0)`：把 redo 数据复制到各 home block，并对每个 home buffer `bunpin()`；
4. 把 `lh.n=0`，再次 `write_head()` 擦除磁盘 header。

真正的原子提交点是第 2 步日志头持久化，而不是第一块 redo data 写入。

| 崩溃位置 | 重启看到的 header | 恢复结果 |
|---|---|---|
| 写 redo data 前/中 | 按该协议应为 `n=0` | 忽略不完整 redo，新批次整体丢失 |
| header 提交后、安装前 | `n>0` 且 block 列表完整 | 重放全部 redo blocks |
| 安装部分 home blocks 后 | `n>0` | 再次重放全部；复制是幂等的 |
| 清 header 后 | `n=0` | home blocks 已全部安装，无需重放 |

该保证建立在 header block 写入原子、且同步 `bwrite()` 的完成顺序能代表所需持久化顺序这两个假设之上。代码的 CPU fence 只约束共享内存和 virtqueue 可见性，不等于磁盘 flush；驱动没有发送 `VIRTIO_BLK_T_FLUSH`，日志也没有校验和或 torn-sector 检测。因此这一结论适用于本仓库的 QEMU 教学配置，不能直接推广为真实硬件掉电语义。

## 11. 启动恢复

恢复不能从 `main()` 直接执行，因为 `bread()/bwrite()` 可能睡眠，必须已有当前进程。真实启动链是：

```text
main -> userinit -> scheduler -> forkret(first)
  -> fsinit(ROOTDEV) -> readsb -> initlog -> recover_from_log
  -> ireclaim -> kexec("/init", ...)
```

`forkret()` 的 `static first` 使这条初始化路径只执行一次。`fsinit()` 调用 `initlog()`，后者在任何用户态文件系统操作开始前执行 `recover_from_log()`：

```text
read_head
  -> install_trans(1)
  -> lh.n = 0
  -> write_head
```

header 为零时安装循环为空。header 非零时逐项输出准确格式 `recovering tail %d dst %d`、复制 redo 到 home block，然后清 header。恢复路径没有运行期 pin，因此 `install_trans(1)` 不调用 `bunpin()`。

日志恢复之后，当前分支还由 `fsinit()` 调用 `ireclaim()` 扫描 orphan inode。redo log 保证磁盘更新原子，但它不会自动发现“目录链接已删除、进程仍持有 inode、随后机器崩溃”的内存引用丢失问题，孤儿扫描负责补上这一层恢复。

## 12. 写入分批与容量耦合

`filewrite()` 不把任意长度写入放进一个事务。对 inode 文件，它计算：

```text
max = ((MAXOPBLOCKS - 1 - 1 - 2) / 2) * BSIZE
```

并将大写入拆成多次 `begin_op -> ilock -> writei -> iunlock -> end_op`。预算为 inode block、可能的 indirect block、数据和 bitmap block 以及非对齐余量留空间。

因此单次 `write()` 对应用程序是一个系统调用，却可能包含多个日志操作区间。这些区间是独立的容量预留边界，不保证对应独立的磁盘 commit：没有其他 outstanding 区间时，每片的 `end_op()` 会提交该片；若并发区间跨过分片边界，前一片和后续片都可能加入同一 group transaction。崩溃只能在实际 group 边界留下已持久化前缀，日志不保证整个大 `write()` 全有或全无。

`MAXOPBLOCKS`、`LOGBLOCKS` 和 `NBUF` 必须联合审查；各更新路径的逐块推导和当前配置下的 cache 余量证明见[文件系统与日志资源上界](resource-bounds.md)：

- 日志至少容纳所有参与者的最坏更新集合；
- cache 至少容纳被日志 pin 的 block 加提交过程临时使用的日志/home buffers；
- `filewrite()` 分块公式必须与文件系统一次 block 分配的实际修改数一致。

当前数值为 `MAXOPBLOCKS=10`、`LOGBLOCKS=30`、`NBUF=30`。提交时除所有 pinned home buffers 外，还要临时取得 log/header/home buffers，因此“最多 30 个日志项”和“正好 30 个 cache 槽”并不构成可任意耗尽的安全余量；正确文件系统路径依赖单操作实际更新集合与 absorption 使 pinned 数保持更低。代码没有启动期断言验证这些容量关系。

## 13. VirtIO 设备初始化

`virtio_disk_init()` 面向 Makefile 以 `-global virtio-mmio.force-legacy=false` 启动的 QEMU modern VirtIO MMIO block device，使用 split virtqueue，不是 legacy transport，也不是通用 VirtIO 驱动。对应规范基线是源码注释引用的 VirtIO 1.1：MMIO transport 见 4.2 节，split virtqueue 见 2.6 节，block device 见 5.2 节。初始化流程：

1. 验证 magic `0x74726976`、version 2、device id 2、QEMU vendor id；
2. 写 0 reset status，随后没有读回等待就依次设置 `ACKNOWLEDGE` 和 `DRIVER`；
3. 不写 feature selector 就读取 feature word；在当前 QEMU reset selector 为 0 的前提下这是低 32 位，随后主动关闭只读、SCSI、writeback config、多队列、任意布局、event index 和 indirect descriptor 等特性；
4. 设置并重新验证 `FEATURES_OK`；
5. 选择 queue 0，确认未 ready 且最大长度至少为 `NUM=8`；
6. 用 `kalloc()` 分别分配并清零 descriptor table、avail ring 和 used ring 页面；
7. 把队列大小和三个物理地址写入 MMIO；
8. 标记所有 descriptor 空闲，设置 queue ready 和 `DRIVER_OK`。

内核 RAM 恒等映射使这些内核指针数值也可作为设备 DMA 物理地址。移植到非恒等映射或有 IOMMU 的环境时不能沿用该假设。

第 2 步没有确认 reset 已在设置 `ACKNOWLEDGE` 前完成。VirtIO 1.1 的通用初始化顺序要求先 reset、再设置 `ACKNOWLEDGE`，MMIO 4.2 规定写 0 触发 reset；但“写 0 后必须等待 `DeviceStatus` 读回 0”这一明确 MUST 位于 PCI 4.1.4.3.2，并不是 1.1 对 MMIO transport 的通用条款，所以不能把当前代码定性为违反该 MMIO MUST。实现仍然缺少 reset-completion 的可观测步骤，并实际假定 reset 的效果在下一次状态写前已经生效；当前 QEMU 同步满足这一假定，异步完成 reset 的实现则需要额外等待/确认路径。

`kernel/virtio.h` 没有定义 `DeviceFeaturesSel/DriverFeaturesSel`，初始化也没有显式选择 feature page。当前 QEMU 在 reset 后让 selector 为 0，才使代码碰到低 32 位；一般合规设备的所选 word 不能据此假定。代码更没有显式读取/写入高 32 位，因此既没从支持白名单构造 feature 集，也没有读取并接受 bit 32 的 `VIRTIO_F_VERSION_1`。严格的 modern 驱动必须协商该位，规范设备可以拒绝当前结果；QEMU 当前组合接受这种简化，不构成通用兼容性保证。失败时驱动直接 panic，也不会给设备设置 `FAILED` status。

### 13.1 split virtqueue 的精确内存布局

queue size 是 `NUM=8`。现代 MMIO 分别接收 descriptor table、driver area（available ring）和 device area（used ring）的 64 位物理地址，三者不要求像 legacy queue 那样拼在一段连续内存。当前驱动为每部分各分配并清零一页；`kalloc()` 的 4096 字节对齐强于 split queue 要求的 16/2/4 字节对齐。

下表的 offset 都相对各自页面基址，字段按 VirtIO modern 接口要求使用 little-endian。当前 RISC-V 构建本身是 little-endian，所以 C 结构可直接共享；代码没有字节序转换，移植到 big-endian CPU 时这一点会失效。

对应的 C 类型名称是 `struct virtq_desc`、`struct virtq_avail` 和 `struct virtq_used`；最后一个类型的 `ring[]` 元素是 `struct virtq_used_elem`。这些结构直接构成驱动与设备共享的二进制布局，修改字段顺序或数组长度时必须重新核对下面全部 offset 和 `sizeof`。

| 区域 | 字节布局 | C 结构跨度 | 规范对齐 | 生产者 -> 消费者 |
|---|---|---:|---:|---|
| descriptor table | 第 `i` 项从 `0x10*i` 开始：`addr +0x0`/8B、`len +0x8`/4B、`flags +0xc`/2B、`next +0xe`/2B | `0x80`（128B） | 16B | driver -> device |
| available ring | `flags @0x00`、`idx @0x02`、`ring[i] @0x04+2*i`，`unused @0x14` | `0x16`（22B） | 2B | driver -> device |
| used ring | `flags @0x00`、`idx @0x02`、`ring[i].id @0x04+8*i`、`len @0x08+8*i` | C 结构 `0x44`（68B） | 4B | device -> driver |

`virtq_avail.unused` 正好占据协商 `VIRTIO_RING_F_EVENT_IDX` 时的 `used_event` 位置；未协商时，base available ring 只使用前 20 字节。该 feature 被驱动拒绝，所以字段保持零且不参与协议。used ring 的可选 `avail_event` 应位于 `0x44..0x45`；C 结构没有为它命名，规范含该字段时区域为 70 字节，但 backing page 覆盖该地址且 feature 已拒绝，设备和驱动都不得依赖它。available `flags` 从清零后保持零，驱动没有请求抑制 used-buffer 中断；驱动也不检查 device-owned `used.flags`，即使设备建议抑制 notify，仍会为每次提交写 `QUEUE_NOTIFY`。

### 13.2 队列账本与所有权

`struct disk` 同时保存三块设备可见内存和纯驱动账本：

| 成员 | 写入者 | 用途 |
|---|---|---|
| `desc` | 驱动写、设备读 | `NUM` 个 `virtq_desc {addr,len,flags,next}`；通过 `NEXT` 串成链 |
| `avail` | 驱动写、设备读 | `flags`、16 位 `idx` 和链头 `ring[NUM]` |
| `used` | 设备写、驱动读 | `flags`、16 位 `idx` 和完成项 `{id,len}` |
| `free[NUM]` | 驱动 | descriptor 空闲位图，1 表示可分配 |
| `used_idx` | 驱动 | 已消费到的 used ring 位置 |
| `info[NUM]` | `b` 仅驱动；status 由驱动初始化、设备写回 | 按链头索引关联请求 buffer 与一字节完成状态 |
| `ops[NUM]` | 驱动写、设备读 | 按链头索引保存请求 header |

初始化阶段无并发。投入运行后，驱动对 queue、`free/info/ops` 和 `b->disk` 的 CPU 访问由 `vdisk_lock` 串行化；设备 DMA 不取得这把 CPU 锁，而是按 split-ring 协议读 driver-owned 区域、写 device-owned 区域和 status byte，双方靠发布顺序及 fence 交接所有权。`NUM=8` 且一次请求固定占三个 descriptor，所以最多只能有两笔请求同时在设备侧 in flight，余下两个 descriptor 不足以组成第三条链。

## 14. 一次 VirtIO block 请求

`virtio_disk_rw(b, write)` 把 xv6 的 1 KiB block number 换算为 512-byte sector：

```text
sector = b->blockno * (BSIZE / 512)
```

这里还有一个由 C 类型决定的边界：`b->blockno` 是 32 位 `uint`，常量因子当前为整型 2，所以乘法先按 32 位无符号算术完成，再扩展赋给 64 位 `sector`。`blockno >= 2^31` 时结果会在扩展前按模 `2^32` 回绕，并映射到较低 sector；把左值写成 `uint64` 并不会让乘法自动变成 64 位。当前可信镜像只有 2000 个 block，不触及该范围，但扩大 block-number 空间时必须先把操作数显式提升到 64 位，并同时增加 capacity/range 校验。

随后在 `disk.vdisk_lock` 下申请三个不要求连续的 descriptor：

```text
[0] struct virtio_blk_req  --NEXT-->
[1] b->data                --NEXT-->
[2] one-byte status
```

- descriptor 0 指向 16 字节 `virtio_blk_req`：little-endian `type @0`、`reserved @4`、`sector @8`，告诉设备读/写类型和起始 sector；
- descriptor 1 在磁盘读时带 `VRING_DESC_F_WRITE`，表示设备写内存；磁盘写时设备读取内存；
- descriptor 2 始终由设备写入状态字节，初始化为 `0xff`，成功为 0；规范的 I/O error 为 1、unsupported 为 2，当前驱动把任意非零值都当作 panic。

若不足三个 descriptor，`alloc3_desc()` 释放本轮已取得的部分并返回失败，调用者在 `&disk.free[0]` 睡眠后从头重试。`free_desc()` 每释放一个 descriptor 都在同一通道唤醒等待者；唤醒不授予所有权，循环重试负责处理竞争和无效唤醒。

请求者设置 `b->disk=1`，把 buffer 记录在以链头索引的 `disk.info[]`，再以当前 16 位 `avail.idx` 为 producer sequence，把链头放入 `avail.ring[avail.idx % NUM]`。第一个 sequentially-consistent fence 位于 descriptor/ring entry 与 `avail.idx++` 之间，承担“先填写内容、后发布 idx”的屏障；第二个位于新 `idx` 与 MMIO notify 之间，承担“先发布 idx、后通知”的屏障。

这是当前 little-endian、cache-coherent QEMU virt machine 上的实现契约，不是仅凭 ISO C fence 就能对所有 DMA 平台作出的保证。真实非一致性 DMA、弱 I/O ordering 或启用 `VIRTIO_F_ORDER_PLATFORM` 的移植还需平台 DMA cache maintenance 和 I/O barrier。两处 fence 更不负责磁盘介质持久化，不能替代 block flush/FUA。

设备维护自己的 available consumer sequence，驱动不读取它；描述符只在完成后才释放，因此 descriptor 空闲表同时限制未消费 available entries。最多两笔请求 in flight，远低于八个 ring slot，driver 不会覆盖设备尚未消费的 available slot。

通知 queue 0 后，请求者在 `b` 通道上睡眠，循环等待 `b->disk` 被完成中断清零。

## 15. 完成中断和 descriptor 回收

设备以自己的 16 位 used producer sequence 选择 `used.ring[idx % NUM]`，先写 `{id,len}`，再提高 `used->idx`，经 PLIC 产生 `VIRTIO0_IRQ`。驱动的 `disk.used_idx` 是对应 consumer sequence。完整入口按 trap 来源分成两条：

```text
kernelvec -> kerneltrap -> devintr -> plic_claim
uservec   -> usertrap   -> devintr -> plic_claim
                              -> virtio_disk_intr
                              -> plic_complete
```

`virtio_disk_intr()`：

1. 获取 `vdisk_lock`；
2. 读取 interrupt status 的低两位并写入 ACK，确认已处理这些 VirtIO 中断原因；
3. 比较本地 `used_idx` 与设备 `used->idx`，在 fence 后读取已发布的 used element；
4. 取完成项的链头 `id`，验证对应 status 为零；
5. 通过链头找到 `struct buf *`，清 `b->disk` 并 `wakeup(b)`；
6. 增加本地 `used_idx`，释放锁。

中断处理程序不释放 descriptor 链。原请求进程醒来、重新获得 `vdisk_lock` 后清 `info[].b`、调用 `free_chain()`，最后返回 `bread/bwrite` 调用者。这样 descriptor 生命周期始终由提交请求的同步路径收尾，而中断只发布完成。

设备可以乱序完成不同请求，因为驱动没有协商 `VIRTIO_F_IN_ORDER`；used entry 的 `id` 而非完成顺序负责找回链头和 `info[id]`。设备返回的 `used_elem.len` 被忽略，读请求也不验证设备是否报告写满 `BSIZE` 数据和 status。代码还不验证 `id < NUM`、`info[id].b != 0` 或 descriptor 链是否确属 in-flight 请求。因此可信 QEMU 之外的损坏、截短或恶意完成可能留下未完全更新的数据，或造成越界/空指针访问，而不是干净返回 I/O error。

`avail.idx`、`used->idx` 和本地 `used_idx` 都是 16 位 sequence，会按模 `2^16` 回绕，并非无限单调；只有选择八个 ring slot 时再对 `NUM` 取模。驱动以 `used_idx != used->idx` 判断是否仍有完成项，每消费一项也按 16 位加一。在最多两笔在途请求、且生产者不能超前消费者 65536 项的当前队列约束下，回绕不会产生“相等但仍积压一整圈”的歧义。

## 16. 锁与睡眠关系

| 层 | 锁 | 睡眠通道 | 发布者 |
|---|---|---|---|
| cache 身份/LRU | `bcache.lock` | 无；无槽直接 panic | `brelse/bunpin` 降引用 |
| 单 block 内容 | `b->lock` sleeplock | sleeplock 对象 | 前一使用者 `brelse` |
| 日志元数据 | `log.lock` | `&log` | `end_op` 释放预留或完成 commit |
| VirtIO 队列 | `vdisk_lock` | `&disk.free[0]`、`b` | 链释放、设备完成中断 |

关键规则：

- `bget()` 不持 `bcache.lock` 等待 buffer sleeplock；
- `end_op()` 不持 `log.lock` 执行 commit；
- `virtio_disk_rw()` 用 `sleep(chan, &vdisk_lock)` 原子释放队列锁，允许中断处理程序取得同一把锁；
- 请求期间调用者仍持有 `b->lock`，再取得 `vdisk_lock`；中断处理程序只取得 `vdisk_lock` 而不反向取得 buffer sleeplock，所以设备操作的 buffer 内容不会被其他进程修改；
- `log_write()` 的完整顺序是调用者已持有 `b->lock`，函数再取 `log.lock`，新 block 路径继续取 `bcache.lock` 调用 `bpin()`；对当前 `kernel/bio.c` 与 `kernel/log.c` 的所有路径核查后，没有发现相反的 `bcache.lock -> log.lock` 顺序；
- `write_log()` 与 `install_trans()` 会先持有一个 log buffer 的 sleeplock，再取得 home buffer 的 sleeplock。合法日志布局保证两者不是同一 block；损坏 header 则可能破坏这一前提并造成自锁。

## 17. 失败和边界行为

- cache 全部有引用：`bget` panic，而不是等待；通常说明日志/cache 参数不一致或 buffer 泄漏。
- descriptor 暂时不足：请求睡眠，链释放后重试；没有超时。
- VirtIO 设备/feature/queue 不符合预期或队列内存不足：启动 panic。
- 设备返回非零 status：完成中断 panic；没有向文件系统传播可恢复 I/O error。
- 驱动不读取设备 capacity，也不检查请求 sector 是否越界；边界完全依赖可信 `fs.img` 的 superblock 和 QEMU 磁盘配置。
- VirtIO interrupt status 的低两位都会被 ACK，但实现只消费 used ring；configuration-change 中断没有独立重配置流程。
- 磁盘 block/inode 耗尽：文件系统尽量返回短写或 `-1`，日志协议仍应保持结构一致；相关路径并非完整 POSIX 语义。
- 设备丢失中断：等待请求永久睡眠；没有轮询 fallback/watchdog。
- 日志 header 损坏：`read_head()` 不验证 `n` 和目标块。`n > LOGBLOCKS` 时，源端 `lh->block[i]` 和目标端 `log.lh.block[i]` 都立即越过声明的 30 项数组；目标端从第一项越界写起就已破坏内核内存，`n` 再大到超过磁盘块可容纳的整数数目时，源端还会越过整个 1024 字节 buffer。随后安装循环还可能读取/写入任意 block。`1 <= n <= LOGBLOCKS` 也可用恶意目标制造越界 I/O 或让 log/home buffer 别名自锁。负 `n` 的行为不同：两个 `i < n`/`tail < n` 循环都执行零次，恢复代码随后把 `n` 清零并重写 header，不会走正数越界的数组复制。教学实现对三类情况都不报告可恢复的镜像错误。
- 日志没有 abort 路径。操作在返回错误前已经 `log_write()` 的局部修改仍会随本批 commit，调用者必须让这些中间状态本身保持文件系统一致。

## 18. 验证

### 18.1 正常与并发 I/O

- `usertests writetest`：小文件重复写读；
- `usertests writebig`、`usertests bigfile`：直接/间接 block 与大文件；
- `usertests manywrites`：四个进程反复写文件，重点暴露 VirtIO 死锁；
- `logstress f0 f1 f2 f3 f4 f5`：多个进程持续加入日志批次；
- `stressfs`、`grind`：混合路径、分配、读写和进程并发。

当前 `user/logstress.c` 自身有一个测试可信度限制：全局 `buf` 只有 `BUFSZ=500` 字节，却以 `SZ=2000` 执行 `memset` 和 `write`，存在越界访问。它仍可能在当前地址布局下给日志施压，但在修复测试程序前，不应把失败唯一归因于存储栈，也不应把通过视为内存安全证明。

### 18.2 崩溃恢复

宿主运行：

```sh
./test-xv6.py log
./test-xv6.py crash
```

`test-xv6.py` 的 crash 阶段会删除并重建 `fs.img`、运行 `user/logstress.c`，在写入过程中向 QEMU 进程发送 `SIGKILL`，再用同一镜像启动 QEMU 并寻找行首 `recovering`。`crash` 还覆盖 `user/forphan.c` 和 `user/dorphan.c` 产生的孤儿 inode。该方式验证本仓库的提交顺序与恢复路径，但 QEMU 进程被杀不等价于真实介质突然掉电，也不覆盖宿主 page cache 的断电丢失。

这类测试会主动终止 QEMU，不能用普通 `usertests` 替代。

### 18.3 建议断点

```gdb
break bget
break log_write
break commit
break write_head
break install_trans
break virtio_disk_rw
break virtio_disk_intr
break alloc3_desc
break free_chain
```

一次写事务中观察 `log.outstanding`、`log.lh.n`、buffer `refcnt`、`b->disk`、`disk.free`、`disk.avail->idx`、`disk.used->idx` 和 `disk.used_idx`。commit 前同一 home block 的多次 `log_write` 应只增加一次 `lh.n/refcnt`；完成安装后 pin 应被逐项释放。若在 `virtio_disk_rw()` 的 sleep 前后单步，应确认睡眠时暂时释放、醒来时重新取得 `vdisk_lock`，而 buffer sleeplock 始终由原请求进程持有。
