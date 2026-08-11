# 信任边界与失败模型

xv6 以教学清晰性为主要目标，不是面向恶意磁盘、恶意设备或多租户攻击者的加固内核。本文集中定义当前代码信任什么、验证什么，以及违反前提后究竟会返回错误、终止进程、panic 还是产生部分副作用。各专题文档中的“正常路径”只有在本模型范围内才成立。

## 1. 保护目标与非目标

当前实现希望保持的核心目标：

1. 普通用户进程不能直接读写 supervisor-only 页或其他进程物理页。
2. 非法用户指针和常见用户异常只终止/拒绝当前请求，不应破坏内核。
3. 已遵守内部前置条件的进程、文件和页表生命周期不发生双重释放或悬空使用。
4. 在当前 QEMU block 模型和日志容量前提下，已提交的文件系统 group 可在进程强杀后重做。
5. 多 hart 正常执行下，锁保护的内核状态保持一致。

明确不是当前目标：

- 抵抗恶意构造的 `fs.img`、ELF 或 VirtIO device response；
- 提供用户/组、权限、capability、sandbox、ASLR、NX 栈策略管理或进程审计；
- 提供 side-channel、speculation、DMA/IOMMU 或物理攻击隔离；
- 在真实存储器断电、写缓存、torn sector 下提供数据库级持久性；
- 对所有资源耗尽都优雅返回错误；
- 对内核栈溢出、死锁、驱动异常或内部 helper 误用做恢复。

## 2. 信任域

```text
用户寄存器/用户页/系统调用参数       不可信
用户命名的路径和普通文件内容         不可信数据，但路径实现应安全解析
用户 ELF                             只适合本仓库构建产物，不是加固解析器
fs.img 元数据与日志头                高度可信
QEMU virt、PLIC、UART、VirtIO DMA     可信设备/模拟器
工具链、链接器、mkfs、QEMU loader     可信构建和启动链
内核内部 helper 的参数               通常可信，违反前置条件可 panic
锁、引用和状态发布协议               由内核调用图保证，不由类型系统强制
```

同一字节可以跨越多个域。例如用户通过 `write()` 提供的普通文件内容不可信，但复制完成后 buffer identity 和日志登记属于可信内核状态；从磁盘再次读回时，文件内容仍可不可信，而 dinode 的 block address 却被内核当作可信元数据使用。

## 3. 失败结果统一词汇

| 结果 | 含义 | 典型入口 |
|---|---|---|
| `-1` | 请求被拒绝或下游失败；不保证无副作用 | bad fd、路径失败、fork/exec/open 资源失败 |
| `0`/短计数 | EOF、零工作量或部分传输 | file/pipe read，部分 copy failure |
| killed | 当前进程稍后以 `-1` 退出 | 无法修复的用户 trap、lazy fault OOM |
| panic | 内核不变量或受信环境前提被破坏 | remap、无 inode/buffer、设备 status 异常 |
| 睡眠 | 等待资源/条件，不是失败 | pipe、console、wait、日志、VirtIO |
| 部分副作用 | 返回失败但内存/持久状态已有变化 | `O_CREATE` 后 fd 失败、大 write、逐页 copy |
| 静默限制/数据丢弃 | 没有错误通道 | console 满后丢输入、忽略部分设备字段 |

文档中的“原子”必须注明原子范围：单个锁临界区、单个日志 group、单次状态发布或整个系统调用不是同一概念。

## 4. 用户态边界

### 4.1 系统调用号与整数参数

`ecall` 保存用户现场后，dispatcher 从 trapframe 的 `a7` 取调用号。当前实现先把 64 位值窄化为 `int`，再检查正数、数组范围和非空表项；高 32 位非零但低 32 位合法的值可能别名到已有系统调用。正常 ABI 不产生这种值，但它不是“完整 64 位调用号必须精确匹配”的验证器。

`argint()` 同样只保留参数槽的低 32 位。具体 handler 还可能继续窄化为 `short` 或转成 `uint`，因此负长度、major/minor 和超大 tick 等边界必须按最终类型分析。

### 4.2 用户地址

`argaddr()` 只取得数值，不验证映射、范围或权限。实际访问必须通过 `copyin()`、`copyout()` 或 `copyinstr()`：

| helper | 页要求 | lazy fallback | 失败副作用 |
|---|---|---|---|
| `copyin` | 实际只查 `V|U`，不查 R/X 或叶项类型 | 可以 | 内核目标可已有前缀，页可已物化 |
| `copyout` | `V|U|W` | 可以 | 用户目标可已有前缀，页可已物化 |
| `copyinstr` | 实际只查 `V|U`，并要求窗口内找到 NUL | 不可以 | 目标可有未终止前缀 |

三个 helper 都逐页推进，不是事务。当前页表构造器给普通用户 leaf 至少加 R，因此 `copyin/copyinstr` 的弱检查在正常页表上不暴露差异；一旦加入 execute-only 页、手工 PTE 或敌对页表输入，就必须在 helper 内补足 leaf/R 校验。`copyin/out(len==0)` 不访问地址，因而坏地址也可能返回成功；调用者不能把它当成独立的地址验证 API。

lazy fallback 还隐含 `pagetable == myproc()->pagetable`。`vmfault()` 虽接收页表形参，范围检查和最终映射却依赖当前进程。临时 exec 页表只有在目标页已 eager 映射、绝不会进入 fallback 时才能传给 copy helper。

即使 helper 返回规则相同，各 backend 的提交点也不同：

| 路径 | copy 失败前后的状态 |
|---|---|
| inode read | 用户缓冲区可能已有前缀，但 `readi()` 把结果改为 `-1`，open-file offset 不前进；重试会从原 offset 再复制 |
| pipe read | 只在每个字节 `copyout()` 成功后增加 `nread`；首字节失败为 `-1`，部分失败返回前缀长度，失败字节保留 |
| console read | 先增加 `cons.r` 再 `copyout()`；失败字节已经丢失，首字节失败返回 0，因而会伪装成 EOF |
| pipe write | 每字节先 `copyin()` 再增加 `nwrite`；失败返回已写前缀，首字节失败返回 0 |
| inode write | 已完成的 chunk 和当前 `writei()` 前缀可修改块、inode size 与 file offset，但 `filewrite()` 只要没有完成全部请求就最终返回 `-1` |
| `sys_pipe` 返回 fd 数组 | 第一个 int 可完整保留，第二个跨页失败时还可写出 1 至 3 字节前缀；内核关闭两端，整个用户数组均无效 |

所以“用户指针非法”不对应统一的 all-or-nothing 语义；必须沿具体调用者同时核对返回值、资源状态、offset/环形计数和已复制前缀。

### 4.3 用户异常

可恢复异常只有当前实现识别的 load/store lazy page fault。权限 fault、instruction fault、非法指令或越界 fault 设置 killed，随后让普通进程退出。`initproc` 是明确例外：同一条路径最终调用 `kexit()` 时会触发 `panic("init exiting")`，不会发布 init zombie。内核态异常也通常 panic；内核没有通用 exception table 或 fault recovery。

kill 是协作式请求，不是同步撤销。`kkill()` 只置位并把 `SLEEPING` 改成 `RUNNABLE`，不会向正在另一个 hart 用户态执行的目标发送专用 IPI；目标通常到下一次 timer/device trap 或系统调用边界才观察 killed，因此延迟依赖时钟中断持续到达，不能承诺固定“几条指令”。被强行唤醒的路径若自身不检查 killed，还可能重新睡下；VirtIO、sleeplock 和日志等待通常要等真实条件完成，console、pipe、`pause` 和 `kwait()` 的等待循环则有显式 killed 检查。即使有检查，检查结束到 `sleep()` 取得 `p->lock` 之间仍有取消窗口；恰好落入该窗口的 kill 可能要等下一次条件唤醒才被观察。

## 5. ELF 与 exec

### 5.1 当前验证

loader 检查 ELF magic，并对每个 `PT_LOAD` 检查：

- `memsz >= filesz`；
- `vaddr + memsz` 不发生已检查形式的回绕；
- segment 起点页对齐；
- 用户页分配和文件读取成功。

新页表在提交前私有，正常可返回失败会完整丢弃新映像并保留旧映像。

### 5.2 未验证或只隐含验证

当前 loader 不完整验证 class、endianness、machine、ABI/version、ELF type、`phentsize`、header table 范围、`off+filesz`、segment overlap、`paddr`、entry 是否位于可执行页，以及 segment 是否避开所有内部高地址边界。它按本地结构大小读取 program header；局部 `off` 是 32 位 `int`，`loadseg()` 又把 ELF64 的 `ph.off/ph.filesz` 传入 32 位 `uint`。超大字段会在真正读 inode 前截窄，而不是得到完整 64 位范围检查。

段布局也不是逐段精确映射。`uvmalloc()` 从当前最大 `sz` 连续分配到本段末端，给 gap 也建立用户映射；它总加 R，只从 ELF flags 提取 W/X。逆序或重叠段复用已有页且不重算权限，`loadseg()` 仍可从内核覆盖已有页内容。因而 loader 适合本仓库产生的递增、非重叠小型 ELF，不适合敌对二进制。

异常布局可能得到三类结果：干净返回 `-1`、在内部 trusted helper 触发 panic、exec 成功后第一次取指被 kill。不能笼统写成“非法 ELF 会被拒绝”。

exec 初始寄存器 ABI只承诺新 PC、SP、`a0=argc`、`a1=argv`。其余 GPR 可能保留旧值；首进程的未指定寄存器还可能来自 allocator poison。没有 `envp`、auxv、TLS 或动态链接器初始状态。

## 6. 文件系统镜像

### 6.1 superblock 与区域

`fsinit()` 只检查 magic。它不集中验证：

- `size/nblocks/ninodes/nlog` 的算术关系；
- log/inode/bitmap/data 区域递增、互不重叠并落在设备容量内；
- `nlog >= LOGBLOCKS+1`；
- bitmap 覆盖所有有效块且保留元数据块；
- 磁盘结构与当前内核编译常量一致。

后续宏直接使用这些字段计算 block number。magic 正确但布局损坏可产生越界 I/O、区域互相覆盖或 panic。

### 6.2 log header

启动 recovery 在把 header 复制到内存前没有先验证 `0<=n<=LOGBLOCKS`，也不验证 home block 范围、重复项或与 log 区重叠。正的 `n>LOGBLOCKS` 会在 `read_head()` 中越界写 `log.lh.block[]`；负 `n` 则使复制和安装循环都为空，随后被静默清成 0。范围内的恶意 target 可把 log data 安装到任意受信 block；若 target 与当前 log source 是同一 buffer，`install_trans()` 还可能在已持有该 buffer sleeplock 时再次 `bread()` 同一 identity 而自锁。

### 6.3 dinode、bitmap 与目录

内核运行期没有完整 fsck 层：

- 非零非法 block address 可指向 metadata、log 或设备范围外；
- bitmap 错误清除 metadata bit 后，allocator 可重新分配并清零元数据；
- 重复 block ownership 可让一个文件的写破坏另一个文件；
- 错误 `nlink==0` 可被 `ireclaim()` 当成真正 orphan 删除；
- 非法目录 inode number、重复名字、错误 `.`/`..` 或目录环只受到局部检查。

完整一致性规则和离线检查顺序见[文件系统一致性](../filesystem/filesystem-consistency.md)。

### 6.4 普通文件内容与元数据的区别

普通文件字节不可信，但安全读取它们本身不应破坏内核。ELF loader、shell/parser 或用户程序负责解释内容。磁盘中的 block pointer、type、size、nlink 和 dirent inode number 则被内核当作元数据；当前实现对它们的验证远弱于用户指针边界。

## 7. 日志与持久化模型

redo log 的原子性建立在以下环境前提上：

1. 每个高层 operation 的 unique home blocks 不超过预算。
2. 已登记 buffer 在 commit 前保持 pin 且不被身份复用。
3. log data 写完成顺序先于非零 header。
4. 非零 header 写被当作完整 block 的提交点。
5. home install 完成顺序先于清 header。
6. 重启能读到与上述完成顺序一致的设备状态。

但一个 xv6 block 是 1024 字节，VirtIO sector 是 512 字节；真实设备可在两个 sector 之间 torn write。驱动不发送 FLUSH/FUA，也不查询/控制宿主缓存。当前 crash 测试只证明 QEMU 进程强杀与 raw image 组合下的经验行为，不证明真实掉电持久性。

系统调用返回也不等于持久化：多个 outstanding operation 组成 group，某个 operation `end_op()` 后可以在其他参与者仍运行时先返回。系统没有 `fsync`、每进程 durability token 或提交 epoch。

## 8. 设备、MMIO 与 DMA

### 8.1 PLIC/UART

内核信任 QEMU 固定的 PLIC context 地址、IRQ 1/10、UART register stride 和 level/edge 行为。PLIC claim id 只按已知编号分派；未知非零 IRQ 只打印并 complete。若未知 level source 仍 asserted，它会立即再次 pending，形成中断风暴，而不是被真正处理。

UART 不 probe 设备能力、时钟、FIFO 或错误状态。输入 overrun、parity、framing 等没有错误上送；console buffer 满时输入可静默丢弃。

### 8.2 VirtIO

当前驱动信任设备写回：

- used id 在范围内并对应真实 in-flight head；
- used length 合理；
- descriptor chain 未被篡改；
- DMA 只访问提供的区域；
- interrupt status 和完成顺序符合 split ring；
- completion status 0 表示完整 block transfer。

代码没有 IOMMU、DMA mapping API 或 hostile-device 隔离。错误 id/len/chain 可能越界访问或错误唤醒；非零 status 直接 panic。驱动还省略完整 feature selector/`VERSION_1` 协商、reset 完成确认、capacity/config generation 和设备重置恢复，只针对当前 QEMU 组合。它不在发请求前检查 block capacity；`b->blockno * 2` 还先按 32 位 `uint` 计算再扩成 64 位 sector，所以损坏元数据给出的超大块号可能回绕并静默访问错误位置，未回绕的越界请求也只能依赖设备 status，最终是 panic 而不是 `EIO`。

CPU fence、MMIO 顺序、DMA coherence 和磁盘持久化是四个不同层次。当前 QEMU coherent RAM 掩盖了真实非一致 DMA 需要的 cache maintenance；普通 C 原子 fence 不能自动提供平台 I/O barrier。

## 9. 启动与工具链

### 9.1 QEMU loader 和 reset handoff

内核依赖 QEMU：

- 把 ELF `PT_LOAD` 放入预期物理地址；
- 清零 `p_memsz-p_filesz`，因为 xv6 没有自己的 BSS 清零循环；
- 从 MROM reset stub 跳到 `0x80000000`；
- 提供稠密、从 0 开始且小于 `NCPU` 的 hart id；
- 交接时让早期 S-mode interrupt 不会在 `stvec` 安装前被递送；
- 提供 Sstc、硬件 A/D 更新、PLIC 和固定 `virt` 内存图。
- 对 `kexec()`/`uvmcopy()` 写入的新指令页表现出当前测试所依赖的 I-cache 一致性；内核没有 `fence.i` 或跨 hart 指令流同步，这不是通用 RISC-V 保证。

内核不解析 device tree，不使用 SBI，也不读回验证 PMP/delegation/Sstc CSR。更换 firmware、board 或 CPU model 不能只保持 `rv64gc` 编译成功。

### 9.2 编译器与链接器

隐含前提包括：

- freestanding C 类型大小、结构对齐和 RISC-V little-endian；
- 当前工具链不为内核 C 生成需要已初始化 `gp` 的 small-data 访问；
- 内核/user linker script、对象顺序和 orphan section 形成预期布局；
- trampoline 恰好一页且 `_entry` 实际位于固定交接地址；
- mkfs 宿主结构布局与内核磁盘结构一致。

这些是构建产物事实，不应只从源码意图推断。版本、命令和产物检查见[平台契约](platform-contracts.md)与[构建链](../build/build-link-and-fs-image.md)。

## 10. 内部 helper 的可信参数

内核 C 类型并未表达大多数前置条件。典型例子：

| helper | 被信任的条件 | 违反结果 |
|---|---|---|
| `kfree()` | 页对齐、范围内、唯一 owner | panic 或 freelist 损坏；双重释放不一定立即发现 |
| `walk()` | `va<MAXVA`，上层有效 PTE 是非叶 | panic 或把数据页当页表 |
| `mappages()` | 对齐、size 非零、目标未映射、PA/权限合理 | panic、部分映射或错误 PTE |
| `brelse()/bwrite()` | caller 持 buffer sleeplock | panic/错误 owner |
| `iput()` | 稳定 inode ref、日志区间、正确锁序 | 回收错对象、日志或调度问题 |
| `log_write()` | 本调用链有 reservation，仍持修改 buffer | 消耗别人预算、panic 或错误版本 |
| `sched()` | 当前 `p->lock` 唯一 spinlock，状态非 RUNNING | panic 或状态破坏 |
| `kexec()` | 内核 argv 最多 `MAXARG-1` 个非空项 | 内部直接调用可越界 `ustack` |
| `vmfault()` | 页表就是当前进程页表，地址低于 `p->sz` 且尚未映射 | 可能检查一个页表却把新页映射进另一个页表；`read` 形参当前被忽略 |

新增调用者必须按[调用上下文契约](../kernel/call-context-contracts.md)审阅完整可达路径，不能因为函数在 `defs.h` 中可见就视为通用安全 API。

## 11. 已知失败表

| 输入/事件 | 当前结果 | 保证边界 |
|---|---|---|
| 非法普通用户指针 | 依 backend 为 `-1`、0 或短计数 | 已复制前缀和 lazy 页不回滚；console 首字节失败还会丢输入并伪装成 EOF |
| 普通进程的用户权限/越界 fault | kill 当前进程 | 内核继续运行；若目标是 init 则升级为 panic |
| 恶意 ELF | `-1`、panic 或稍后 kill | 不保证统一拒绝 |
| 损坏 superblock/log | panic、越界 I/O、静默破坏 | 只信任正常 mkfs 镜像 |
| 物理页 OOM | 依路径 `-1`/kill/panic | 见资源失败矩阵 |
| inode/buffer cache 耗尽 | panic | 不是用户可恢复资源错误 |
| VirtIO 非零 status | panic | 无 `EIO`/retry/reset |
| console overflow | 丢弃输入 | 无错误报告 |
| kill 阻塞 I/O | 有的取消，有的继续等待 | 不是统一 cancellation API |
| QEMU 强杀 | 日志/orphan 恢复尝试 | 不等于真实设备掉电 |

## 12. 加固优先顺序

1. 启动时在任何 recovery/I/O 前验证 superblock、log header 和区域算术。
2. 为文件系统提供离线 checker，验证 block ownership、bitmap、inode/link 和目录图。
3. 让 ELF parser 验证完整 header、table 范围、segment overlap、reserved VA 和 executable entry。
4. 把所有用户长度/地址 API 的类型和负值语义统一，避免依 backend 偶然转换。
5. 为 VirtIO 做 feature 白名单、selector、reset、used id/len/chain 校验、capacity 和错误上送。
6. 为 buffer/inode 等固定 cache 提供等待/backpressure 或明确的 reserved headroom，而不是 panic。
7. 把内部前置条件转成断言、静态检查、owner pin 或调试 lockdep。
8. 若面向真实硬件，定义 DMA cache、I/O fence、`fence.i`、flush/FUA 和 torn-write 模型。

## 13. 验证原则

- 用户边界：测试返回值、进程存活、内核存活和部分副作用四项，而不只看 panic。
- 资源边界：记录失败前后的引用/页/块账本，并验证资源归还后能再次成功。
- 磁盘边界：使用可控 crash point 和离线 checker，不依赖固定延时撞窗口。
- 并发边界：通过 gate 控制线性化点，再用压力测试扩大覆盖；一次随机成功不是证明。
- 平台边界：保存完整工具版本与 `readelf/objdump/nm` 证据；源码常量不能证明运行平台能力。

具体实验见[故障注入](../verification/fault-injection.md)，当前测试能证明和不能证明的项目见[追踪矩阵](../reference/source-test-traceability.md)。
