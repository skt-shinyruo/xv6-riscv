# 文件系统一致性、离线检查与损坏模型

本文为当前仓库的 `fs.img` 定义一套可执行的一致性规范，并给出一个默认只读的简化 `fsck` 算法。它回答的不是“正常源码想做什么”，而是三个更严格的问题：一个镜像在恢复日志后必须满足哪些关系；当前内核实际检查了哪些关系；检查器如何在不信任 superblock、日志头、块指针和目录项的前提下安全地产生诊断。

相关实现为 [`kernel/fs.h`](../../../kernel/fs.h)、[`kernel/fs.c`](../../../kernel/fs.c)、[`kernel/log.c`](../../../kernel/log.c)、[`kernel/bio.c`](../../../kernel/bio.c) 与 [`mkfs/mkfs.c`](../../../mkfs/mkfs.c)。运行期对象、事务和设备路径分别见[文件系统](../kernel/filesystem.md)、[存储栈](../kernel/storage-stack.md)和[一次文件系统事务](../flows/filesystem-transaction.md)。本文所依赖的跨层前提见[全局正确性不变量](../correctness/global-invariants.md)，恶意镜像与掉电不属于当前内核保护目标这一事实见[信任与失败模型](../architecture/trust-and-failure-model.md)。

## 1. 判定对象与术语

检查对象必须是一个停止写入后的镜像快照。不得在 QEMU 仍打开同一个 `fs.img` 时运行检查或修复；宿主文件缓存、QEMU block 请求与检查器读取并不构成一致快照。

本文区分三层结论：

| 层次 | 含义 | 例子 |
|---|---|---|
| 安全一致 | 内核读取、分配或回收时不会因该结构覆盖无关对象 | 所有块指针在 data 区，且没有重复 owner |
| 逻辑一致 | 命名、链接计数和可达性相互吻合 | 普通文件 `nlink` 等于指向它的目录项数 |
| 规范化 | 没有当前失败路径允许留下、但并不立即危险的冗余 | EOF 后没有多余块，空 dirent 的名字全零 |

因此“检查通过”应至少有两种模式：

- `safe`：没有 `FATAL` 或 `ERROR`，允许已明确分类的 orphan 和非规范告警；
- `canonical`：除信息项外没有任何诊断，适合验证 `mkfs` 输出和修复后的镜像。

日志恢复前的 home blocks 可以是同一已提交事务的新旧混合，不能直接拿来做结构检查。本文所说的“一致镜像”默认指：先把合法的非零日志在内存 shadow view 中完整重放，再检查得到的逻辑视图。

## 2. 当前磁盘 ABI

### 2.1 基本类型与结构大小

磁盘格式由宿主 `mkfs` 生产、由 little-endian RISC-V 内核消费。`mkfs` 用 `xshort()`/`xint()` 显式写 little-endian，但内核直接把磁盘字节解释为本地 C 结构，因此消费者还依赖当前目标为 little-endian、`short` 为 2 字节、`uint/int` 为 4 字节以及结构没有额外 padding。

| 对象 | 当前大小 | 关键布局 |
|---|---:|---|
| filesystem block | 1024 B | `BSIZE` |
| `struct superblock` | 32 B | 八个连续 `uint` |
| `struct dinode` | 64 B | 四个 16-bit 字段、`size`、13 个块地址 |
| `struct dirent` | 16 B | 2-byte `inum` + 14-byte `name` |
| `struct logheader` | 124 B | 4-byte `n` + 30 个 4-byte home block number |

由此得到：

```text
IPB       = BSIZE / sizeof(dinode) = 1024 / 64 = 16
BPB       = BSIZE * 8              = 8192 bitmap bits
NINDIRECT = BSIZE / sizeof(uint)   = 256
MAXFILE   = NDIRECT + NINDIRECT    = 12 + 256 = 268 blocks
MAXBYTES  = MAXFILE * BSIZE        = 274432 bytes
```

`struct logheader` 定义在 `kernel/log.c`，不在共享的 `fs.h` 中；这仍是持久 ABI。修改 `LOGBLOCKS`、字段类型、结构顺序或对齐后，旧镜像与新内核不再自动兼容。`mkfs` 只断言 `sizeof(int)==4`、`BSIZE` 可整除 dinode 和 dirent 大小，没有把上述所有 ABI 条件编码成构建期检查。

### 2.2 当前 2000-block 布局

当前参数为 `FSSIZE=2000`、`NINODES=200`、`LOGBLOCKS=30`。`mkfs` 的实际公式是：

```text
nlog         = LOGBLOCKS + 1           = 31
ninodeblocks = NINODES / IPB + 1       = 13
nbitmap      = FSSIZE / BPB + 1        = 1
nmeta        = 2 + nlog + ninodeblocks + nbitmap = 47
nblocks      = FSSIZE - nmeta           = 1953
```

当前具体布局如下。区间使用左闭右开表示：

| 区域 | block 区间 | 数量 | 内容 |
|---|---:|---:|---|
| boot | `[0,1)` | 1 | 内核文件系统代码不解释 |
| superblock | `[1,2)` | 1 | 前 32 字节为 superblock |
| redo log | `[2,33)` | 31 | block 2 为 header，3..32 为 30 个 data slots |
| dinode table | `[33,46)` | 13 | inode 0..199 位于此处，尾部还有未使用槽 |
| bitmap | `[46,47)` | 1 | bit `b` 描述 filesystem block `b` |
| data | `[47,2000)` | 1953 | indirect、目录和普通文件数据块 |

superblock 的严格当前值为：

```text
magic=0x10203040, size=2000, nblocks=1953, ninodes=200,
nlog=31, logstart=2, inodestart=33, bmapstart=46
```

有效 inode 号是 `[1,200)`；inode 0 保留，根目录是 inode 1。`sb.nblocks` 是整个 data 区块数，不是当前空闲块数。

`mkfs` 中 `wsect()` 的参数名和注释使用 sector 一词，但其偏移是 `sec * BSIZE`，这里的单位实际是 1024-byte filesystem block。VirtIO 驱动才把一个 filesystem block 换成两个 512-byte sectors。

### 2.3 通用区域公式与严格 profile

离线工具不能先信任字段、再用宏计算偏移。所有加法、乘法和向上取整都应在至少 64-bit 无符号类型中做 checked arithmetic：

```text
inode_span_min  = ceil(ninodes / IPB)
bitmap_span_min = ceil(size / BPB)
log_end         = checked_add(logstart, nlog)
inode_min_end   = checked_add(inodestart, inode_span_min)
bitmap_min_end  = checked_add(bmapstart, bitmap_span_min)
data_start      = checked_sub(size, nblocks)
image_bytes     = checked_mul(size, BSIZE)
```

superblock 没有直接存 `data_start`，但 `nblocks` 明确给出 data 区长度，所以必须先验证 `nblocks<=size`，再由 `size-nblocks` 得到实际起点。不能用 `bmapstart+ceil(size/BPB)` 反推它：`mkfs` 的 `/ + 1` 写法在参数恰好整除时会比数学上的 `ceil` 多预留一块。通用结构检查要求最小 inode/bitmap 覆盖落在各自边界内，并把 `log_end..inodestart`、`inode_min_end..bmapstart`、`bitmap_min_end..data_start` 的任何 gap 都保留为 reserved；只有 `[data_start,size)` 是 data。当前严格 profile 仍要求区域连续并精确等于上一节数值。

## 3. 生产者保证与消费者验证的差距

### 3.1 `mkfs` 实际做了什么

`mkfs` 先把 2000 个 block 全部清零，再写 superblock、根目录和命令行文件，最后把 `[0,freeblock)` 的 bitmap bits 设为已用。新建 dinode 初始 `nlink=1`；根目录先写 `.` 和 `..`，两者都指向 inode 1。输入名会去掉 `user/` 前缀和一个前导 `_`，并断言最终字符串不含 `/` 且长度不超过 14。

这些步骤不足以构成通用镜像验证器：

1. 规范化后的输入名没有做重复检查，`_name` 与 `name` 等组合可以产生同名 dirent。
2. `freeinode` 没有与 `NINODES` 比较，`freeblock++` 也没有与 `FSSIZE` 比较；超量输入可能越过已规划区域。
3. `balloc()` 只写第一个 bitmap block，并只断言最终 `used < BPB`；它依赖当前 `FSSIZE < BPB`。
4. 根目录 size 被设置为 `((old_size / BSIZE) + 1) * BSIZE`。当前通常只是把最后一个部分块扩展到块尾；若 `old_size` 已恰好对齐，则会额外增加一个未分配逻辑块，产生 size 内空洞。
5. `mkfs` 不检查目录图、nlink、重复 block ownership 或生成后的全局 bitmap 等式。

### 3.2 内核启动和运行期实际检查

`fsinit()` 只检查 `sb.magic == FSMAGIC`，随后立即按 `sb.logstart` 恢复日志。其他关键路径同样只做局部检查：

| 对象 | 当前检查 | 没有检查 |
|---|---|---|
| superblock | magic | 区域算术、重叠、设备容量、参数匹配 |
| log header | `sizeof(logheader)<BSIZE` 是编译配置检查 | 磁盘 `n` 范围、target 范围/重复/指向 log |
| dinode | `ilock()` 拒绝 `type==0` | 非法非零 type、size、nlink、块地址 |
| bitmap | `bfree()` 检查目标 bit 已置位 | metadata 保留、引用完整、重复 owner |
| block map | logical index 不超过 `MAXFILE` | 非零物理地址是否在 data 区 |
| dirent | lookup 跳过 `inum==0` | inum 范围、目标已分配、特殊项、重复名 |
| unlink | 目标 `nlink>=1`，目录表面为空 | 全局 nlink 等式、目录环、多父目录 |

尤其危险的是：`balloc()` 从 block 0 开始相信 bitmap；若 metadata bit 被错误清零，它可把 superblock、日志或 inode block 当作空闲块并 `bzero()`。`readi()` 又调用会分配的 `bmap()`；size 内的零地址不是稀疏洞，而可能使只读调用在事务外分配、触发 `log_write outside of trans` panic，或把分配混入另一个全局 outstanding group。

所以后文的规则多数是“允许内核安全消费的良构前提”，不是当前内核已经证明或拒绝的输入条件。

## 4. Superblock 完整规则

检查器必须先只读取固定 block 1 的前 32 字节，并按 little-endian 解码。任何依赖 `logstart/inodestart/bmapstart` 的读取都必须等本节通过后进行。

### 4.1 必须成立

1. 镜像长度至少为两个完整 block，且 `magic==FSMAGIC`。
2. `size>=2`，`size*BSIZE` 不溢出且不大于快照长度；严格 profile 要求恰好等于快照长度，尾随字节在通用模式下至少是告警。
3. `ninodes>ROOTINO`，且 inode 1 的槽位能落入经验证的 inode 区。
4. `nlog>=LOGBLOCKS+1`，使当前内核可能访问的 header 与 30 个 data slots 都在日志区；严格 profile 要求 `nlog==31`。
5. `nblocks<=size`，`logstart>=2`，`log_end<=inodestart`，`inode_min_end<=bmapstart`，`bitmap_min_end<=data_start=size-nblocks`；严格 profile 要求这些边界全部相等、没有 gap。
6. `[inodestart,bmapstart)` 至少容纳 `ceil(ninodes/IPB)` blocks，`[bmapstart,data_start)` 至少容纳 `ceil(size/BPB)` blocks；多出的块是 reserved gap，不进入 allocator data 区。
7. data 区严格由 superblock 定义为 `[size-nblocks,size)`；当前严格 profile 还要求 `size==FSSIZE`、`nblocks==1953` 和所有起点精确匹配。
8. 所有区域端点计算均未溢出，且其 byte offsets 在快照中可读。

### 4.2 失败含义

任何 geometry 失败都是 `FATAL_GEOMETRY`：停止日志重放和结构遍历。继续使用损坏字段不仅会产生误报，还可能让检查器自己越界读取。可以额外输出固定位置的十六进制供取证，但不得给出“其他部分 clean”的结论。

## 5. 日志头、恢复视图与提交状态

### 5.1 合法日志头

日志头的 `n` 是有符号 32-bit little-endian 值。合法条件为：

```text
0 <= n <= min(LOGBLOCKS, nlog - 1)
```

当 `n==0` 时，后面的 block numbers 和日志 data 都是无效残留，不参与一致性判断。当 `n>0` 时，对每个 `i in [0,n)`：

1. redo source 是 `logstart+1+i`，必须仍在已验证日志区。
2. `block[i]` 必须属于已验证的 dinode、bitmap 或 data region；正常运行不会日志化 boot、superblock、任何 log block 或区域间 gap。当前严格连续布局下，这个并集恰好是 `[inodestart,size)`。
3. targets 必须两两不同；运行期 log absorption 保证合法 header 不含重复 home block。
4. target 可以是 dinode、bitmap、indirect、目录或普通数据 block，但不得与任何 redo source 相同。

磁盘 `n<0` 时当前 recovery 循环会跳过并清头；`n>30` 时 `read_head()` 会在边界检查前复制过量数组项。离线检查器两者都必须判为 `FATAL_LOG_HEADER`，不得模仿这些不安全行为。

### 5.2 只在 shadow view 中重放

对合法 `n>0`，检查器构造 copy-on-write 视图：

```text
view(block) = redo[block]  if block 是 header 中某个 target
              base[block]  otherwise
```

所有后续 dinode、bitmap、indirect 和目录读取都来自 `view`。原镜像保持字节不变，header 也不在检查期间清零。检查报告必须同时记录：

- `PENDING_COMMITTED_LOG n=N`：逻辑检查针对重放后状态；
- raw home 状态可能不一致，这是合法 crash window，不单独报损坏；
- 若 shadow view 不一致，错误属于已提交的最终状态或日志数据损坏。

如果日志头非法，任何“猜测前 N 项后重放”的做法都不安全。应停止，要求人工取证或来自可信备份的恢复。

## 6. Dinode 与块映射规则

### 6.1 inode 状态

inode 0 必须保持未分配。对 `[1,ninodes)` 中每个 dinode：

- `type==0` 表示 free。正常删除后 `nlink/size/addrs` 应为零；device inode 被删除后 major/minor 可以保留旧值，因为 `iput()` 只清 type。free inode 中的非零块指针不是 owner，必须报错，不能据此占有或释放块。
- `type` 非零时只能是 `T_DIR=1`、`T_FILE=2` 或 `T_DEVICE=3`。
- `nlink` 按有符号 16-bit 解释且不得为负。
- `size<=MAXFILE*BSIZE`。
- `T_DIR.size` 必须是 `sizeof(dirent)=16` 的整数倍。
- `T_DEVICE` 在当前实现中应有 `size==0` 且全部 `addrs==0`；设备 I/O 从不使用 inode data。违反说明镜像非规范且在 unlink 时可能释放意外块。

### 6.2 合法物理块

每个非零 direct pointer、indirect block pointer 和 indirect entry 都必须位于 `[data_start,size)`。指向 boot、super、log、dinode 或 bitmap 区的地址是 `FATAL_BLOCK_RANGE`，即使它在 `sb.size` 内也不合法。

所有非零 pointer 都进入全局 owner map：

```text
owner[block] = (inum, DIRECT[j])
             | (inum, INDIRECT_BLOCK)
             | (inum, INDIRECT[j])
```

同一 block 第二次出现即为 `FATAL_DUP_BLOCK`。间接块自身和它引用的数据块是两个独立 owner；间接块不能同时被另一个 inode 当数据块使用。

### 6.3 size、空洞和 EOF 后块

令 `required=ceil(size/BSIZE)`。当前文件系统不支持 sparse file：每个 `logical_bn in [0,required)` 必须有非零映射。size 内空洞是 `FATAL_HOLE`，因为普通 `readi()` 可能在事务外分配它。

size 之外的非零 pointer 要继续纳入 owner/bitmap 检查，但标为 `WARN_SURPLUS_BLOCK`。它并非纯理论情况：`writei()` 先调用 `bmap()`，再从用户页复制；copyin 或后续分配失败时，inode 可能持久化一个 EOF 后数据块或空的 indirect block，而 size 没有前进。只要地址合法、唯一且 bitmap 已置位，这一状态浪费空间但不会让 allocator 重用仍被 inode 指向的块。canonical 模式要求清除它，safe 模式不应误报为重复或 bitmap leak。

对 free inode 中的块地址则相反：`type==0` 使内核不再承认其所有权，`ialloc()` 会直接清零 dinode。此类地址必须报告 `ERROR_FREE_INODE_POINTER`，相应 bitmap block 只能在完成全局 owner 扫描后作为 leak 处理。

## 7. Bitmap 与全局块所有权

在 inode 指针全部解析成功后，再核对 bitmap：

1. `[0,data_start)` 的所有 metadata/reserved bits 必须为 1。任一清零都是 `FATAL_METADATA_FREE`，因为 `balloc()` 会从 block 0 开始扫描并可能清零该区域。
2. owner map 中每个 data block 的 bit 必须为 1。引用存在而 bit 清零是 `FATAL_BITMAP_MISSING`；下一次分配可能让两个对象共享同块。
3. data 区中 bit 为 1、但 owner map 没有记录的块是 `ERROR_BITMAP_LEAK`。只有在所有 inode/indirect block 都成功扫描后才能安全下此结论。
4. `[size,bitmap_span*BPB)` 的尾部 bits 不被当前 allocator 使用；严格 mkfs 输出应为 0，非零记 `WARN_BITMAP_TAIL`，不计入可分配空间。
5. orphan inode 指向的块仍有合法 owner，不能当 leak。先分类 orphan，再决定是否释放。

安全等式可以写为：

```text
bitmap_allocated_within_size
  = reserved_blocks union all_nonzero_pointers_of_allocated_inodes
```

两边还必须是无重复集合。该等式不区分逻辑 file data 与 EOF 后 surplus；后者仍是一个实际持久 owner。

## 8. 目录记录、名字与图结构

### 8.1 单个目录的记录规则

只有在目录 inode 的块映射已验证无空洞后才能读取 dirents。遍历 `[0,size)` 中每个 16-byte 记录：

- `inum==0` 表示空槽；当前创建/删除路径会让其余 14 字节也为零，非零残留只记规范化告警。
- `inum!=0` 时必须满足 `1<=inum<ninodes`，且目标 dinode 的 type 非零。
- name 是固定 14 字节，不保证 NUL 结束。若含 NUL，NUL 后字节应为零；若不含 NUL，全部 14 字节都是名字。
- 名字不得为空或包含 `/`。普通名字不得等于 `.` 或 `..`。
- 重名按内核 `strncmp(...,DIRSIZ)` 语义判断：比较遇到首个 NUL 停止，否则比较全部 14 字节。不能仅比较原始 14-byte 数组，否则 `"a\0x"` 与 `"a\0y"` 会被错误视为不同。
- 同一目录中的每个有效名字必须唯一；重复名造成 pathname lookup 依物理顺序选中一个目标，应判 `FATAL_DUP_NAME`。

### 8.2 `.`、`..` 与目录树

每个已分配目录必须恰有一个 `.` 指向自己、一个 `..` 指向其父目录。只有根目录的 `..` 指向根自身；其他可达目录的目标必须等于后续图检查确定的唯一 parent。

把所有非 `.`/`..` 且目标为 `T_DIR` 的 dirent 视为 parent-to-child edge。对根可达部分必须成立：

1. root inode 已分配且 type 为 `T_DIR`。
2. root 没有来自普通 dirent 的父边；每个其他可达目录恰有一个父边。
3. child 的 `..` 等于该唯一父目录。
4. DFS 不出现灰色回边，目录图无环。
5. 目录硬链接不存在；同一 child 不能从两个名字或两个父目录进入。

`sys_link()` 禁止目录目标，`sys_unlink()` 禁止 `.`/`..` 且只允许删除空目录，所以这些是合法运行的良构结果，不只是 Unix 风格偏好。目录环会同时破坏路径终止、parent nlink 语义和自动修复的可判定性，必须视为 `FATAL_DIR_CYCLE`，不得自动“任选一条边删除”。

### 8.3 可达性

从 root 沿所有普通 dirents 做图遍历。普通文件可由多个名字到达；目录只能形成树。分类如下：

| 状态 | 判定 |
|---|---|
| reachable | 至少一个从 root 开始的合法名字路径可达 |
| normal orphan | 不可达、`nlink==0`，且没有普通 dirent 指向；目录 orphan 还必须除 `.`/`..` 外为空 |
| lost allocated inode | 不可达但 `nlink>0`，或不可达目录仍有子项 |
| dangling reference | dirent 指向 free/越界 inode |

最后两类不可能由受支持的完整日志事务产生，分别是 `ERROR_LOST_INODE` 和 `FATAL_DANGLING_DIRENT`。

## 9. `nlink` 的精确等式

`nlink` 不等于所有指向 inode 的 dirent 数，因为目录的 `.` 不计数，而一个子目录的 `..` 通过显式增加 parent `nlink` 体现。

先统计：

```text
ordinary_refs(i) = 所有目录中，非 "."/".." 且 target==i 的 dirent 数
child_dirs(d)    = 所有普通目录边中，parent==d 的边数
```

合法值为：

| inode 类别 | 期望 `nlink` |
|---|---:|
| reachable regular/device | `ordinary_refs(i)` |
| reachable non-root directory | `1 + child_dirs(i)`，且 `ordinary_refs(i)==1` |
| root directory | `1 + child_dirs(ROOTINO)`，且没有普通父边 |
| normal orphan regular/device | 0 |
| normal orphan empty directory | 0 |

等价地，非根目录的基础 1 来自父目录中的名字；每个直接子目录再为自己的 `..` 让 parent 加 1。目录自己的 `.` 不增加自身 nlink。

`nlink` 不匹配通常是 `ERROR_NLINK`；以下情况升级为 `FATAL_RECLAIM_HAZARD`：一个普通 dirent 仍指向 `nlink==0` 的 allocated inode。当前启动顺序在日志恢复后无条件运行 `ireclaim()`；对没有既存 cache 引用的非根 inode，`iget()+iput()` 满足最后引用条件，会截断并清 type，随后留下悬空目录项。root 是实现特例：`userinit()` 已在 `fsinit()` 前把它作为 cwd pin，reclaim 临时引用使 `ref==2`，所以该轮 `iput()` 不会删除 root。这个偶然 pin 不让错误 root nlink 变成合法镜像，危险分类仍保持 fatal。

## 10. Orphan 的边界

合法 orphan 来自：文件或空目录已 unlink 并提交，但某个打开 file/cwd 的内存引用仍存在，系统在最后 `iput()` 前崩溃。重启后内存 ref 消失，磁盘表现为：

```text
type != 0
nlink == 0
没有普通目录项指向
块指针合法、唯一且 bitmap 已置位
若为目录，只剩 "." 和 ".." 及空槽
```

这应报告为 `RECOVERABLE_ORPHAN`，不是 bitmap leak。当前内核先重放日志，再由 `ireclaim()` 为每个候选建立新事务并走 `iput()`；正常未缓存 orphan 会执行 `itrunc()` 并清 type。预先由 init cwd pin 的 root 不满足最后引用条件，是上述明确例外。

不要把所有不可达 inode 都叫 orphan：`nlink>0` 的不可达 inode、仍被 dirent 指向的 `nlink==0` inode、非空不可达目录或含非法块的 inode 都不是自动回收安全对象。特别是错误 nlink 不能用“内核本来也会回收”作为修复依据。

## 11. 默认只读的简化 `fsck`

### 11.1 安全前置条件

1. 停止所有使用镜像的 QEMU，复制或取得不可变快照。
2. 以只读方式打开；每次读取使用 checked byte range，不 mmap 后按未验证指针索引。
3. 记录镜像 hash、长度、检查器版本和内核 ABI profile。
4. 默认不调用内核 recovery，也不改原始 header；所有重放进入内存 overlay。

### 11.2 检查顺序

顺序本身是安全条件：

```text
phase 0  snapshot and fixed-size file checks
phase 1  decode and validate superblock geometry
phase 2  validate log header and build replay overlay
phase 3  decode every dinode; validate size/type/pointers
phase 4  build block owner map; then cross-check bitmap
phase 5  parse directory records from validated mappings
phase 6  validate names, dot/dotdot, parents and cycles
phase 7  compute reachability, nlink and orphan classes
phase 8  emit deterministic report; make no writes
```

提前检查 bitmap 再解析全部 indirect blocks会把合法 owner 误报为 leak；先沿损坏 superblock 或 log target 读块则可能让检查器越界；在 shadow replay 前检查 home blocks会把正常 crash window 误报为不一致。

### 11.3 伪代码

```text
check(path, profile):
  base = open_read_only_snapshot(path)
  require base.length >= 2 * BSIZE

  sb = decode_superblock(base.block(1))
  if !valid_geometry_checked(sb, base.length, profile):
    emit(FATAL_GEOMETRY); return UNSAFE

  lh = decode_log_header(base.block(sb.logstart))
  if !valid_log_header(lh, sb):
    emit(FATAL_LOG_HEADER); return UNSAFE

  view = cow_overlay(base)
  if lh.n > 0:
    for i in 0 .. lh.n-1:
      view.replace(lh.block[i], base.block(sb.logstart + 1 + i))
    emit(PENDING_COMMITTED_LOG, lh.n)

  reserve owner[0 .. data_start-1] as METADATA
  inode = array(sb.ninodes)
  for inum in 0 .. sb.ninodes-1:
    inode[inum] = decode_dinode(view, checked_inode_offset(inum))
    validate_type_size_nlink(inode[inum])
    if inode[inum].type != 0:
      validate_all_nonzero_addresses(inode[inum])
      read_validated_indirect_if_present(inode[inum])
      add_unique_owners_or_fatal(owner, inode[inum])
      require_no_holes_inside_size(inode[inum])

  bitmap = decode_bitmap(view, sb)
  compare_reserved_and_owners_with_bitmap(bitmap, owner)

  for each allocated T_DIR inode d:
    entries[d] = read_dirents_only_through_validated_map(view, d)
    validate_entry_ranges_and_unique_names(entries[d], inode)

  graph = build_directory_graph(entries)
  require_root_and_dot_rules(graph)
  detect_multiple_parents_and_cycles(graph)
  reachable = traverse_from_root(graph, all ordinary entries)
  compare_nlink_counts(inode, entries, graph)
  classify_orphans_and_lost_inodes(inode, reachable, entries)

  emit_sorted_diagnostics()
  return severity_exit_code()
```

实现应在累计到局部错误后尽量继续，但不得用无效对象派生地址。例如一个 indirect pointer 越界后，可以继续扫描其他 inode，却不能读取该 indirect block，也不能宣称全局 bitmap leak 集合完整；报告需标记 `OWNER_SCAN_INCOMPLETE`。

### 11.4 诊断等级与退出码

| 等级 | 建议退出码 | 含义 |
|---|---:|---|
| `CLEAN` | 0 | safe/canonical profile 均通过 |
| `INFO/WARN` | 1 | pending log、surplus block、非零尾 bit 等；仍可只读分析 |
| `RECOVERABLE` | 2 | 只有严格定义的 orphan 或可证明的 unowned bitmap leak |
| `ERROR` | 3 | nlink、可达性或规范关系错误，不能自动挂载 |
| `FATAL` | 4 | geometry、日志、越界/重复块、目录环或回收危险；禁止启动 |
| `INCOMPLETE` | 5 | I/O/read failure 或前置损坏使全局结论无法完成 |

报告项至少包含 code、对象类型、inum/block/dir offset、观察值、期望值、证据来源是 base 还是 replay overlay，以及由此跳过的后续检查。

## 12. 修复策略

默认行为必须是只读。修复只能针对副本生成新镜像，流程为：保存原始 hash -> 生成 repair plan -> 用户确认 -> 写临时完整镜像 -> `fsync` 文件 -> 重新运行完整 checker -> 原子替换目标并 `fsync` 目录。不得在原镜像上逐字段就地试错。

建议修复优先级：

1. 合法非零日志：先把 shadow view 物化到新镜像，再清 header。这不是“修复损坏”，而是完成已提交 recovery。
2. 只有在块 owner 扫描完整时，才可重建 bitmap。先置上所有 referenced/reserved bits，再考虑清除证明为 unowned 的 bits。
3. 严格 normal orphan 可释放其 direct/indirect/data blocks并清 dinode；任何目录引用存在时禁止这样做。
4. 只有目录图完整、无环、无 dangling entry 时，才可从目录关系重算 nlink。
5. EOF 后 surplus 可在复制镜像中解除 pointer 并释放对应块；必须同时处理 indirect block 是否变空。

不得自动修复：重复块 owner、目录环、多父目录、重复名字、含内容的 lost directory、非法 log target，以及无法判断哪一半较新的 torn block。重复块不是简单“清一个 bit”：需要选择语义 owner，必要时复制数据并原子更新 inode 与 bitmap；检查器没有足够信息代替人工决策。

## 13. 512-byte sector torn write 与持久化限制

VirtIO 请求一次传输 1024-byte filesystem block，起始 sector 为 `blockno*2`。日志证明把每个 `bwrite()` 当作完整、按调用顺序持久的 block 写，但真实持久化至少有三层缺口：

1. 一个 1024-byte block 由两个 512-byte sectors 组成，掉电可留下前半新、后半旧，或相反。日志 data 和 home block 都没有 checksum，replay 会忠实安装这个混合块。
2. 驱动没有发送 `VIRTIO_BLK_T_FLUSH` 或 FUA，也没有查询 writeback cache。请求 completion 证明设备模型已完成请求，不证明数据已到断电稳定介质；设备/宿主可能让 header 先于较早的 log data durable。
3. `SIGKILL` QEMU 只丢失 QEMU RAM，并保留宿主仍能看到的 raw image。它不模拟宿主断电、page cache 丢失、控制器缓存重排或 sector tear。

当前 log header 的有效内容只有 124 bytes，恰好都落在 header block 的第一个 512-byte sector；所以“只撕裂第二 sector”不会改变 `n/list`。这只是当前布局的偶然属性：log data、home data仍会撕裂，第一个 sector 自身也可能发生更细粒度破坏，而且无 flush 时顺序仍不成立。

没有 generation、checksum 或双写 header 时，离线 fsck 也不能可靠识别所有 torn writes。普通文件数据由新旧两个合法半块拼接，在结构上完全合法；一个旧的完整 log data block配上已持久的新 header也可能形成结构合法但语义过时的状态。此时 checker 最多报告“无法证明内容新鲜”，不能从单一镜像恢复正确版本。

## 14. 损坏传播路径

| 原始损坏 | 当前内核可能结果 | fsck 必须阻止/报告 |
|---|---|---|
| `bmapstart` 指入 log | allocator 把 log bytes当 bitmap | geometry overlap，停止 |
| log `n>30` | `read_head` 越界写内存 | 非法 header，绝不重放 |
| log target 指向 header | recovery 把 log data 写进 header，覆盖提交协议 | target region violation |
| log target 等于本轮 source `logstart+1+i` | recovery 持 source sleeplock 后再次 `bread()` 同一 buffer，自锁 | target region violation，绝不重放 |
| metadata bitmap bit 清零 | `balloc+bzero` 清除元数据 | `FATAL_METADATA_FREE` |
| inode 指向另一 inode 的 block | 一方写入破坏另一方内容 | `FATAL_DUP_BLOCK` |
| size 内地址为 0 | 只读路径尝试分配并写日志 | `FATAL_HOLE` |
| dirent inum 越界/free | `iget/ilock` 读错误位置或 panic | `FATAL_DANGLING_DIRENT` |
| reachable inode nlink 为 0 | 启动 `ireclaim` 通常删除仍有名字的非根 inode；root 因 cwd pin 暂时逃过但仍损坏 | `FATAL_RECLAIM_HAZARD` |
| 目录环/多父 | 路径与父关系无简单树语义 | 禁止自动修复/启动 |
| bitmap 置位但无 owner | 永久丢失空间 | repairable leak，仅在全扫描后判定 |

## 15. 精确损坏注入测试

所有测试从新建的只读基线副本开始，确保 on-disk log `n==0`。每个 case 只改列出的字段，运行 fsck 后丢弃副本；不同损坏不得累计。字段偏移按当前 ABI：

```text
super.field_offset = 1*BSIZE + {magic:0,size:4,nblocks:8,ninodes:12,
                                nlog:16,logstart:20,inodestart:24,bmapstart:28}
log.n              = 2*BSIZE + 0
log.block[i]       = 2*BSIZE + 4 + 4*i
dinode(inum)       = (33 + inum/IPB)*BSIZE + (inum%IPB)*64
dinode.nlink       = dinode(inum) + 6
dinode.size        = dinode(inum) + 8
dinode.addrs[j]    = dinode(inum) + 12 + 4*j
bitmap_bit(b)      = byte 46*BSIZE + b/8, mask 1<<(b%8)
```

不要在通用测试工具中硬编码 33/46/47；它应先通过 geometry 后派生这些值。它还应通过解析目录找到测试 inode，而不是假定用户程序排列固定。下表的 baseline block numbers只适用于当前干净 `mkfs` profile。

| Case | 精确变更 | 预期诊断/行为 |
|---|---|---|
| SB-overlap | 把 `bmapstart` 写成 2 | `FATAL_GEOMETRY`；不得读取 block 2 作为 bitmap |
| SB-size | 把 `size` 写成 2001，文件长度不变 | `FATAL_GEOMETRY`/short image |
| LOG-count | 把 log `n` 写成 31 | `FATAL_LOG_HEADER`；不得复制第 31 个 target |
| LOG-target | `n=1, block[0]=2` | `FATAL_LOG_TARGET`；不得 shadow replay |
| LOG-oob | `n=1, block[0]=2000` | `FATAL_LOG_TARGET` |
| LOG-duplicate | `n=2, block[0]=block[1]=33` | `FATAL_LOG_DUP_TARGET` |
| bad direct | 对 size>0 的 regular inode，把 `addrs[0]` 写成 1 | `FATAL_BLOCK_RANGE`，不得把 superblock 当数据 |
| out of range | 把同一地址写成 2000 | `FATAL_BLOCK_RANGE`，不发出该 block read |
| duplicate owner | 令两个 size>0 文件的 `addrs[0]` 相同 | `FATAL_DUP_BLOCK`，报告两个 `(inum,slot)` |
| missing bitmap | 清除某个 active data block 的 bitmap bit | `FATAL_BITMAP_MISSING`；说明 allocator reuse 风险 |
| leaked bitmap | 对一个无 owner 的 data block置 bit | `ERROR_BITMAP_LEAK`；owner scan 完整时可计划修复 |
| oversized inode | 把 regular inode size 写成 274433 | `FATAL_INODE_SIZE` |
| hole | 把 size>0 inode 的首地址清零，不改 size | `FATAL_HOLE`；原块随后另报 bitmap leak |
| bad dirent | 把一个 active root dirent 的 inum 写成 200 | `FATAL_DANGLING_DIRENT` |
| duplicate name | 把两个 active root dirents 的 14-byte name 设为相同 | `FATAL_DUP_NAME` |
| bad regular nlink | 给普通文件 nlink 加 1 | `ERROR_NLINK`，显示 observed/ordinary_refs |
| false orphan | 保留 root dirent，却把目标 nlink 写成 0 | `FATAL_RECLAIM_HAZARD`；明确禁止用该镜像启动 |
| valid orphan | 清除唯一普通 dirent、把目标 nlink 写成 0，保留合法 blocks/bits | 仅 `RECOVERABLE_ORPHAN`；模拟 reclaim 后应 clean |
| bad root parent | 把 root 的 `..` inum 改为另一目录 | `FATAL_DOTDOT` |
| directory cycle | 先创建 `/a/b/tmp` 再 unlink `tmp`，确认 `b` 的 size 内留下空槽；把该槽写成 `{inum=a,name="back"}` | `FATAL_DIR_CYCLE`，通常同时报告 multiple parent |

### 15.1 Shadow replay 必须生效的测试

取一个 clean inode home block 33，复制到 log data block 3，只在这个 log copy 中把某个文件 nlink 加 1；保持 home block 不变，再写 `n=1, block[0]=33`。预期：

1. checker 报 `PENDING_COMMITTED_LOG n=1`；
2. `ERROR_NLINK` 的 evidence 标记为 replay overlay；
3. 若 checker 直接检查 home block而未见错误，测试失败；
4. 原始镜像的 log header 和 home block在只读检查后逐字节不变。

再做一个等价内容的合法 pending log：log data 与 home block完全相同、header 指向该 home。预期只有 pending-log 信息，结构仍 safe；物化 recovery 并清 header 后 canonical clean。

### 15.2 512-byte tear 测试

保存同一普通文件数据块的 old/new 两个版本，构造：

```text
torn = new[0:512] || old[512:1024]
```

把 `torn` 放入已提交日志 data slot，header 合法地指向该文件 home block。shadow replay 后结构检查仍可能 clean，但文件内容是混合版本。预期报告中必须说明“无 checksum，内容 tear 不可判定”；测试不得期待 fsck 凭结构恢复 old 或 new。

对目录或 dinode block做相同拼接时，若两半恰好仍分别包含合法记录，检查也可能无法识别事务世代不一致；若拼接造成 nlink/bitmap/dirent 关系破坏，则应由对应全局规则发现，而不是报一个并不存在证据的通用 `TORN_WRITE`。

### 15.3 无 FLUSH/FUA 的顺序测试

固定延时 `SIGKILL` 不能验证 durable order。要验证模型边界，需要在 block backend 或驱动测试层加入可控 gate：记录请求完成，但把 log data、header、home、clear 四类写暂存并按指定顺序选择性落到测试镜像。至少覆盖：

1. 只落部分 log data、不落 header：恢复应忽略，旧状态保留。
2. 落全部 log data和 header、不落 home：恢复应完整重放。
3. 落 header但故意丢一个较早 log data：证明当前协议在 completion 不等于 durability 时可能重放旧数据；fsck不一定能发现。
4. 落部分 home、保留 header：重放应幂等收敛，前提是 log data完整。
5. 落全部 home、丢 clear header：再次重放应保持相同结构。
6. 落 clear header、故意丢一个 home：证明缺少 flush/order 可留下无日志可恢复的不一致。

第 3、6 项是负向能力测试：预期不是“xv6 自动修好”，而是明确复现当前持久化前提被破坏后的失效。只有实现 flush/FUA、写入校验与可识别 torn block后，才能把它们升级为正向保证。

## 16. 验收清单

一个符合本文的只读 checker 至少应证明：

1. 对任何 32-byte superblock 字段组合，都不会在 geometry 验证前派生越界 offset。
2. 对任意 32-bit log `n/target`，不会越界复制、写输入镜像或重放到 log/boot/super。
3. committed log 只进入 shadow view，所有结构关系都在该 view 上检查。
4. 每个非零块指针只有一个 owner，metadata 永不进入 data owner 集。
5. bitmap 等式在完整扫描后建立，orphan/surplus 不被误判为 unowned leak。
6. dirent 名字按 14-byte 内核语义比较，目录图能报告环和多父。
7. regular/device 与 directory 使用各自正确的 nlink 公式。
8. reachable `nlink==0` 与 normal orphan 被明确区分。
9. 输出稳定、可机器解析，并说明因前置错误而跳过的证明。
10. 所有检查默认只读；修复写新镜像并在替换前重新验证。

## 17. 核心结论

当前 xv6 的日志能在其教学设备模型下把合法更新 group 收敛到提交前或提交后，但它不是磁盘格式验证器。真正的安全挂载前提是：可信 geometry、合法可重放的日志、无越界或重复块、bitmap 与 owner 集一致、size 内无洞、目录为根树加严格 orphan、nlink 与名字图相符。

这些关系中，当前内核启动只验证了 magic。离线 checker 的价值不只是发现“坏文件”：它必须在 recovery 之前阻止损坏 header把错误数据安装到 home blocks，并在 `ireclaim` 之前阻止错误 nlink 删除仍可达的非根 inode；root 即使因 cwd pin 暂时未删也必须拒绝。对 512-byte tear、缓存重排和无 checksum 的内容损坏，单一镜像没有足够证据；文档和测试必须把这种不可证明性保留下来，而不是把 QEMU `SIGKILL` 的经验结果外推为真实掉电保证。
