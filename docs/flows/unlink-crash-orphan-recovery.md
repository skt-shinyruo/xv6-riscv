# `unlink` 到崩溃后 orphan 回收

本文追踪“文件仍被进程打开时删除最后一个目录链接”的完整生命周期，并在 redo log 的精确 crash point 上区分可见名称、磁盘 `nlink`、内存 `ref` 和数据块所有权。

## 1. 四类状态不能混为一谈

| 状态 | 位置 | 含义 |
|---|---|---|
| directory entry | 目录数据块 | 名称是否可达 inode number |
| `dinode.nlink` | 磁盘 inode | 非 `.` 的目录链接计数；目录另计孩子 `..` 贡献 |
| `inode.ref` | 内存 inode cache | 内核指针/打开 file/cwd/path lookup 临时引用 |
| block bitmap | 磁盘 bitmap | inode 数据/间接块是否仍占用 |

最后一个名称消失只令 `nlink` 变 0。只要 `ref>0`，正常运行内核允许文件继续经已有 fd 读写，不能提前清块。

## 2. `sys_unlink` 的事务

在 `begin_op()` 后：

1. `nameiparent` 取得父目录 `dp`，`ilock(dp)`；
2. 拒绝 `.`/`..`，`dirlookup` 取得 `ip` 和 dirent offset；
3. `ilock(ip)`，验证 `nlink>=1`，目录还必须为空；
4. 用全零 dirent 覆盖父目录槽，`log_write` 登记父目录数据块；
5. 若目标是目录，`dp->nlink--` 并登记父 dinode；
6. 释放父引用；
7. `ip->nlink--`、`iupdate(ip)` 登记目标 dinode；
8. `iunlockput(ip)`；若仍有打开 file ref，不会截断；
9. `end_op()`，最后一个 outstanding operation 负责 commit。

目录项清除与 `nlink` 更新属于同一日志事务。它们可先在 buffer cache 中被同事务/并发受锁代码看到，但 crash 后的磁盘可见性由 log header 决定。

## 3. 正常不崩溃路径

```text
before: name -> inum, nlink=1, ref includes open fd, blocks allocated
unlink commit: name absent, nlink=0, ref>0, blocks still allocated
existing fd: may continue read/write
last close: fileclose -> begin_op -> iput
            itrunc frees direct/indirect blocks, type=0, bitmap updated
            end_op commits reclamation
after: no name, no ref, free dinode and blocks
```

`iput()` 在 `itable.lock` 下检查 `ref==1 && valid && nlink==0`，并在仍持该锁时取得 inode sleeplock，因此检查成立时不会已有其他引用者持有该 sleeplock。随后它释放 `itable.lock` 再截断；此时新的 `iget()` 仍可增加 `ref` 并取得指针，但遵守 inode API 的调用者只有在随后取得 sleeplock 后才能读字段，届时会观察已经清空并置 `valid=0` 的状态。最后的 `ref--` 再受 `itable.lock` 保护，从而保证 cache 槽身份和字段串行化；协议并不禁止回收期间出现新的引用。

## 4. unlink commit 的 crash matrix

令事务包含 `n` 个 unique home blocks，提交顺序是 log data、非零 log header、home install、清 header：

| crash 点 | 启动时日志 | 恢复后的名称/nlink | 后续动作 |
|---|---|---|---|
| 写任意 log data 前/中，header 仍旧为 0 | 无 committed tx | 旧目录项与旧 nlink | 文件仍可通过名称访问；未提交 log data 被忽略 |
| log data 全写但 header 前 | header 0 | 旧状态 | 同上 |
| 非零 header 已持久化 | committed | replay 后名称消失、nlink=0 | `ireclaim` 回收 orphan |
| 只安装部分 home | header 非零 | replay 覆盖所有 home，得到完整新状态 | `ireclaim` |
| 全部 home 后、清 header 前 | header 非零 | 幂等 replay 新状态 | `ireclaim` |
| 清 header 后 | header 0，home 已安装 | 新状态 | `ireclaim` |

“写完成”在当前模型中只表示 VirtIO request completion。`BSIZE=1024` 跨两个 512B sector，且驱动不发 FLUSH/FUA；真实掉电可能 torn/reorder。表格是 xv6 教学日志假设下的结论，严格介质模型见[文件系统一致性](../filesystem/filesystem-consistency.md)和[信任与失败模型](../architecture/trust-and-failure-model.md)。

## 5. 为什么需要启动 `ireclaim`

若机器在 unlink 已提交、最后打开 fd 尚未 close 时崩溃，RAM 中 `inode.ref` 消失。磁盘却留下：

```text
type != 0, nlink == 0, blocks still allocated, no directory entry
```

上游只靠最后 `iput` 的运行时回收会永久泄漏。当前分支 `fsinit(ROOTDEV)` 先 `initlog` 并完成 recovery，再 `ireclaim(dev)` 扫全部磁盘 dinode。顺序不可颠倒：必须先把 committed unlink replay 到 home locations，扫描才看见 nlink 0。

对每个 orphan，`ireclaim` 先在扫描阶段 `iget` 取得引用并释放 dinode buffer，再进入新的 `begin_op/end_op` 执行 `ilock -> iunlock -> iput`；最后一个 ref 触发 `itrunc` 和清 type。每个回收事务受日志容量保护；回收中再次 crash 时，启动 recovery 先完成或忽略该事务，再次扫描，因而趋向完成。

## 6. 幂等性与边界

- 已释放一部分块但事务 header durable 时，replay 统一安装 bitmap/inode 新状态；之后扫描不会再次把已清 type 的 inode当 orphan。
- header 未提交时，home 应保持旧的完整 orphan，下一次扫描重试。
- 这依赖 log header 自身良构；当前 `read_head()` 不验证 `n`、目标范围或重复项，损坏 header 可能把 replay 写到任意块。
- `ireclaim` 把磁盘 `nlink==0` 当真。若该值是静默损坏而名称仍指向 inode，它会删除仍可达数据；离线 fsck 应先做全局 link-count 对照。
- 未链接但 `type!=0` 且 `nlink>0` 的 inode不会被回收，属于泄漏/损坏而不是 xv6 orphan 状态。
- 目录 unlink 只有空目录可通过，且父 `nlink` 更新必须和目标变化同事务；目录环或坏 `..` 不在运行时修复范围。

## 7. 精确 crash 测试

不要用“延时两秒后 SIGKILL”作为唯一方法。测试构建应在 block write completion 后提供单调 crash point：

```text
UNLINK_LOG_DATA(k)
UNLINK_HEADER_COMMITTED
UNLINK_HOME(k)
UNLINK_HEADER_CLEARED
IRECLAIM_LOG_DATA(k) / HEADER / HOME(k) / CLEAR
```

每个点的流程：

1. 从同一干净 `fs.img` 副本启动；创建有可识别内容的文件并保持 fd 打开；
2. 执行最后链接的 unlink，等待指定 checkpoint 后立即终止 QEMU，不做 guest shutdown；
3. 对 crash image 先运行只读 checker，记录原始 log/header/home 状态；
4. 重新启动一次，让 recovery+ireclaim 完成并等待已知启动标记及磁盘请求静止；当前 xv6 无 guest 正常关机接口，由宿主记录并终止这一实例的确切 QEMU pid；
5. 再离线检查：名称与 inode/link count/bitmap 一致，不存在重复块或 `type!=0,nlink==0` orphan；
6. 多次重启结果相同，空闲块数回到预期，其他文件内容哈希不变。

对 sector tearing 的结构负例，应选择会破坏 bitmap、dinode、dirent 或 log-header 关系的 1024B 元数据写，把其中一半替换为旧/新数据并要求 checker 报告不一致。普通文件数据没有 checksum，撕裂后仍可能结构合法；此类 fixture 只能由预先记录的内容 hash 检出，不能伪称 fsck 必然发现。
