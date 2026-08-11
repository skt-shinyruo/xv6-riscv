# 实验：实现只读离线 fsck

实现一个宿主工具检查 `fs.img` 的全局一致性。默认模式只读，先在内存 shadow image中处理 committed redo log，再检查 superblock、inode/block所有权、bitmap和目录图。完整规则以[文件系统一致性](../filesystem/filesystem-consistency.md)为准。

## 1. 安全边界

- QEMU未运行、没有进程以可写方式打开目标镜像；
- 所有 corruption test只操作临时副本；
- 解析任何字段前验证镜像长度和算术溢出；
- 坏 block/inum永远只作为待验证整数，验证前不得用于 seek/数组下标；
- 默认不修复。可选 repair必须显式 flag、先生成备份/计划并在副本上执行。

工具不能链接/调用 guest内核代码后假定宿主布局相同；需要用固定宽度字段、显式 little-endian读取和编译期/运行时尺寸检查。

## 2. 有序验证流水线

顺序本身是安全性要求：

1. **container**：文件长度至少覆盖 block 0/1，且对 `BSIZE` 余数有明确策略；
2. **superblock**：magic、size、nblocks、ninodes、nlog及各 region起止，全部用 overflow-safe算术；
3. **log header**：先验证 `0<=n<=min(LOGBLOCKS, sb.nlog-1, (BSIZE-sizeof(int))/sizeof(int))`；这里 `sb.nlog` 包含 header block。每个 home target还必须在合法范围、非 log region、按策略唯一；
4. **shadow replay**：若 header nonzero，只把有效 log data复制到内存/临时 shadow，不修改源镜像；
5. **dinodes**：type/size/nlink/major/minor和指针基本范围；
6. **block ownership**：展开 direct/indirect pointers，构建 `owner[block]`，检测元数据引用、重复块和size/最大文件冲突；
7. **bitmap**：所有保留/已引用块应置位，置位但无 owner的数据块分类为泄漏；
8. **directories**：size对齐、inum范围、名字终止/重复、`.`/`..`、父关系、环、可达性；
9. **link counts**：从目录引用重新计数，与 dinode.nlink按xv6目录语义比较；
10. **orphans**：区分合法 crash残留 `type!=0,nlink=0,unreachable`、有链接但nlink0的危险损坏、不可达nlink>0泄漏；
11. 输出稳定排序报告和退出码。

不能先 replay未经验证的 log header；当前内核会信任它，但 fsck的目标之一正是安全诊断这种损坏。

## 3. 数据模型

建议保留三份状态：

```text
raw_image       原字节，只读
shadow_blocks   raw + validated committed log overlay
facts           inode refs, block owners, directory edges, bitmap state
```

错误记录包括稳定 code、严重度、对象 id、原始字段值和因果上下文。例如先报告 `E_BLOCK_RANGE inode=7 direct[2]=99999`，之后抑制由该指针派生的 duplicate/bitmap噪声。

退出码建议：0 clean；1一致性错误；2输入/格式无法安全解析；3工具内部错误。仅 warning是否令退出非零需固定在CLI契约中。

## 4. 分阶段任务

### A. 几何和 ABI

先只打印当前镜像布局并与 `mkfs` 公式对照。用 golden fixture验证 superblock字段、dinode/dirent尺寸和当前 2000-block布局。所有 region采用半开区间。

inode 解码只能枚举 `[0, sb.ninodes)`。最后一个物理 inode block 可能还有不可寻址的结构槽；当前 `ninodes=200`、`IPB=16`，槽 200..207 就属于这种 padding。它们不能进入 inode 或 block-owner 账本，严格 profile 可另行要求其字节为零。由 geometry 多预留的完整 inode block则是 reserved gap，同样不是新增 inode 号。

### B. inode与block checker

实现只读 pointer展开。间接块本身和其指向的数据块都记录 owner；同 inode内部重复与跨 inode重复都报错。只读取已验证为 data-region的间接块。

### C. bitmap对照

区分 metadata/reserved、inode-owned和free。当前 `mkfs` 只建立一个 bitmap block的事实不能写死成通用解析规则；依据 superblock size计算所需数量并验证当前geometry。

### D. 目录图

从 ROOTINO做 DFS/BFS。严格 parent 规则只施加于 root 可达图：验证 root `.`/`..` 自指、每个其他可达目录有唯一 parent 且 `..` 指向它，并禁止目录硬链接/环。固定 `DIRSIZ=14` 名称不保证 NUL 终止；解码最多读取 14 字节，名称比较要匹配内核 `strncmp(..., DIRSIZ)` 语义，即遇到 NUL 后名称结束，而不是把 NUL 后的填充字节算入名称。严格模式可以另报 NUL 后的非零尾部。

不可达目录必须先与 ordinary refs 一起分类。若某目录 `nlink==0`、没有普通外部 dirent 指向，且非空记录只有一个自指 `.` 与一个 inum 在合法范围内、但不等于自身的 `..`，它可以是可回收 orphan；旧 parent 可能已 free 或复用为其他类型，所以该 `..` 不要求目标已分配，也不进入 dangling、parent 或 nlink 账本。inum 越界、自指 `..` 或非空 orphan 都不能套用这个例外。所有其他已分配目录仍要验证 `.` 自指、`..` 指向已分配目录，且除 root 外不得自指；只有 root 可达目录再要求后者等于唯一 parent。

`dinode.nlink` 不能简单等于“指向该 inode 的全部 dirent 数”。对 root 可达的良构树，`.` 不计入；一个非根目录在 parent 中的普通名字给它基础 1；每条仍存在的普通 parent-to-child 目录边再给 parent 加 1，root 自身的基础值为 1。该边与 child 的 `..` 在良构树中一一对应，但 checker 不能反过来机械扫描所有物理 `..` 做加法：空目录 unlink 后若仍由 cwd 引用，它会成为 `nlink==0` 的 orphan，磁盘上还保留指向旧 parent 的 `..`，而 `sys_unlink()` 已经把旧 parent 的 `nlink` 减一。这个规则对应 `create()`/`unlink()` 对 `dp->nlink` 的显式加减，也解释了为什么目录创建时写入 `.` 不会再增加自身 `nlink`。checker 应先建立普通目录边和 orphan 分类，再按边重算计数。

### E. shadow log

为 raw view和replayed view分别给摘要。正常 committed log下，replayed view应一致；raw home可能是旧/部分新状态，不应在 replay前误报为最终 corruption。

### F. 可选 repair计划

先只输出机器可读 plan，不写镜像。若实现写入，按“隔离坏引用、重算bitmap/link、处理orphan”的保守顺序，并使用xv6日志兼容的事务或离线原子替换；禁止就地边扫边改。

## 5. 验收条件

- 对干净 `make fs.img` 返回0，重复输出字节相同；
- 对带合法 nonzero committed log的副本，raw摘要与shadow摘要可解释，shadow检查通过且源文件哈希不变；
- 每个 corruption fixture只触发预期主错误码及有限派生错误，不崩溃、不越界读、不卡死；
- 能检测非法块号、metadata指针、同/跨 inode重复块、bitmap漏置/多置、超大size、坏type/nlink、非法inum、重复名、坏`.`/`..`、目录环、不可达inode和坏log header；
- 在 ASan/UBSan宿主构建及截断/随机字节输入上不发生未定义行为；
- 默认模式前后镜像SHA-256完全相同；
- 对工具shadow replay后的结果与启动xv6 recovery后再检查结果，在支持的日志模型内一致。

## 6. corruption fixtures

写一个只操作临时副本的结构化 mutator，用 `(block,offset,width,old,new)` manifest而不是十六进制手改。至少包含：

| fixture | mutation | 期望主诊断 |
|---|---|---|
| bad-magic | superblock magic翻转 | `E_SUPER_MAGIC`，停止深解析 |
| bad-geometry | logstart/size越界或溢出 | `E_SUPER_GEOMETRY` |
| bad-log-n | header n超过容量 | `E_LOG_COUNT`，绝不replay |
| bad-log-target | target指向log/镜像外 | `E_LOG_TARGET` |
| duplicate-block | 两 dinode指向同 data block | `E_BLOCK_DUP` |
| metadata-pointer | direct指向inode/bitmap block | `E_BLOCK_REGION` |
| bitmap-missing | 清已引用块 bit | `E_BITMAP_UNMARKED` |
| bitmap-leak | 置无owner data bit | `E_BITMAP_LEAK` |
| bad-dirent | inum超范围 | `E_DIRENT_INUM` |
| duplicate-name | 同目录两个相同 14-byte name；另做 `a\0x`/`a\0y` 尾部不同但按内核语义同名的 case | `E_DIR_NAME_DUP` |
| bad-dotdot | 把 root 可达、非 root 目录的 `..` 改为非实际 parent | `E_DOTDOT/E_DIR_PARENT`，不能仅凭这项声称出现遍历环 |
| dir-cycle | 插入或改写一个普通 child dirent，使其指向遍历路径上的祖先目录 | `E_DIR_CYCLE`，并允许伴随 multiple-parent 诊断 |
| wrong-nlink | 改计数 | `E_NLINK` |
| orphan | 对普通文件清最后一个 dirent 并置 nlink=0，保留 type/blocks；目录 variant 只能选空目录，且还要同步把原 parent 的 nlink 减 1 | 分类为可回收 orphan，不伴随额外 `E_NLINK` |
| nested-dir-orphan | 在 xv6 中创建 `/a/b`；P 执行 `chdir("/a/b")`，Q 依次 unlink `/a/b` 和 `/a`，然后在 P 退出前取 snapshot | `b` 是可回收 orphan；即使 `b/..` 指向的 `a` 已 free 或复用，也不报 dangling/parent/nlink 错误；模拟 `ireclaim` 后 clean |
| orphan-self-dotdot | 把上一 fixture 的 `b/..` 改为 `b` 自身 inum | `E_ORPHAN_DOTDOT` 等专用异常；不得分类为 normal orphan，可另注明 `ireclaim` 会忽略该项并截断 inode |
| torn-metadata | 为已知 inode/bitmap/log 关系准备镜像，只替换目标 1024B block 的一个 512B sector | 触发该 fixture 预先指定的结构错误，不崩溃、不越界 |
| torn-file-data | 普通文件数据块只替换一个 512B sector | 允许结构检查仍 clean；没有外部 checksum 时必须报告“内容完整性不可证明”，不能声称检测到任意 torn data |

每个 fixture保存基线哈希、mutation manifest、checker版本和期望error code集合。

## 7. crash-point联动

与[故障注入](../verification/fault-injection.md)的 log data/header/home/clear checkpoint组合：每次 crash先对原镜像运行fsck raw+shadow模式，再启动一次xv6 recovery/`ireclaim`，最后复检。不要只验证“系统能启动”；必须验证无重复块、link/bitmap一致和其他文件内容未变。

## 8. 清理与报告

临时镜像放在专用目录，测试结束记录并删除路径，不修改仓库基线 `fs.img`。报告列出支持/不支持的损坏、是否启用repair、退出码契约和当前内核仅检查magic的差距；不要把fsck clean解释为数据内容语义正确或真实介质已持久化。
