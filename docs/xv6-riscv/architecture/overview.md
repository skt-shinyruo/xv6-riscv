# xv6-riscv 总体架构

本文给出当前仓库的全局模型：代码如何分层、CPU 在哪些执行上下文之间切换、主要对象由谁拥有，以及跨模块时必须保持哪些不变量。它是后续模块文档的术语和边界基线。

## 1. 源码地图

本篇直接覆盖以下全局定义文件：

- `kernel/param.h`：固定容量、文件系统事务上限和用户栈页数；
- `kernel/memlayout.h`：QEMU `virt` 机器物理地址与内核/用户高地址布局；
- `kernel/defs.h`：内核各编译单元之间的公共函数接口；

其余实现按职责分布在：

```text
kernel/       supervisor mode 内核、汇编入口和设备驱动
user/         user mode 程序、C 运行库和系统调用 stub 生成器
mkfs/         在宿主机上构造 xv6 磁盘镜像
Makefile      交叉编译、链接、镜像生成和 QEMU 启动编排
test-xv6.py   从宿主机驱动 QEMU、普通测试和崩溃恢复测试
```

## 2. 系统分层

从用户请求到磁盘的主依赖方向如下：

```text
用户程序
  |
  v
用户运行库 / syscall stub
  |
  v  ecall
trap + syscall 分派
  |
  +--> 进程 / 虚拟内存
  |
  `--> 文件描述符层
          |
          +--> pipe
          +--> console device
          `--> inode / 目录 / 路径
                    |
                    v
                buffer cache
                    |
                    +--> redo log
                    |
                    v
                 VirtIO block
```

依赖并非严格单向。例如文件系统等待磁盘时会调用进程层的 `sleep()`，进程退出时又会调用文件层关闭资源。因此正确性依赖明确的锁顺序和“调用时能否睡眠”，不能仅靠目录层次判断。

## 3. 三类执行实体

### 3.1 hart

RISC-V 的 hart 是硬件执行线程。QEMU 通过 `-smp $(CPUS)` 创建多个 hart；每个 hart 先执行 `0x1000` 的 MROM reset stub，再被转交给 xv6 的 `_entry`。`start()` 将 `mhartid` 保存到 `tp`，之后 `cpuid()` 用 `tp` 索引 `cpus[NCPU]`。

访问 `tp`/`mycpu()` 时必须阻止当前内核线程迁移到其他 CPU。代码通过关闭中断实现这一点：`myproc()` 自己用 `push_off()/pop_off()` 包围读取，调用 `mycpu()` 的其他路径必须已经关闭中断。

### 3.2 CPU 调度器上下文

每个 `struct cpu` 保存：

- `proc`：当前在该 hart 上运行的进程；调度器自身运行时为零；
- `context`：调度切换所需的 `ra`、`sp` 和 `s0..s11` 保存区；
- `noff/intena`：嵌套关闭中断的深度及最外层关闭前的中断状态。

调度器不是一个普通用户进程，也没有独立 `struct proc`。每个 hart 从 `main()` 进入 `scheduler()` 后继续使用自己的早期 `stack0` 栈；`swtch()` 第一次离开调度器时把该栈指针保存在 `cpu.context`，并换到目标进程的高地址内核栈。此后进程与该 hart 的调度器上下文通过 `swtch()` 直接交接。

### 3.3 进程

`struct proc` 同时代表：

- 一个用户地址空间；
- 一个 trapframe；
- 一段固定内核栈；
- 一个可由调度器保存/恢复的内核上下文；
- 文件描述符、当前目录、父子关系及退出状态。

同一进程一次只能在一个 CPU 上处于 `RUNNING`。进程睡眠或让出 CPU 时，内核调用栈仍保留在该进程自己的内核栈中，`swtch()` 只显式保存 `struct context` 列出的 `ra`、`sp` 和 `s0..s11`；其余寄存器由调用约定视为调用者易失值。

## 4. 执行模式和边界

系统涉及三种 RISC-V 特权模式：

| 模式 | 本仓库用途 | 典型入口/退出 |
|---|---|---|
| machine | 每个 hart 的早期启动和委托配置 | `_entry -> start() -> mret` |
| supervisor | 内核、trap 处理、调度器和设备驱动 | `uservec`、`kernelvec`、`sret` |
| user | `init`、shell、工具和测试 | 普通用户 ELF 入口 `start()`；`_forktest` 特例直接进入 `main()` |

machine mode 只参与启动。定时器使用 Sstc 的 `stimecmp`，`start()` 允许 supervisor 访问计时器并将异常/中断委托给 supervisor。系统运行后，不依赖 OpenSBI 处理普通 trap。

用户态进入内核时存在一个特殊过渡窗口：硬件已经切到 supervisor mode，但仍使用用户页表和用户 `sp`。`trampoline.S:uservec` 必须先把寄存器保存到固定映射的 `TRAPFRAME`，再换内核栈、`tp` 和页表。

## 5. 地址空间总览

### 5.1 物理地址

`kernel/memlayout.h` 定义本实现依赖的 QEMU `virt` 布局：

```text
0x02000000  CLINT（QEMU 提供；当前 Sstc 定时器路径不访问它）
0x0c000000  PLIC
0x10000000  UART0
0x10001000  VirtIO MMIO block device
0x80000000  KERNBASE，QEMU 加载内核且 reset stub 转交控制的位置
0x88000000  PHYSTOP，本内核使用的 128 MiB RAM 上界
```

内核映像占据 `KERNBASE..end`，物理页分配器管理对齐后的 `end..PHYSTOP`。

### 5.2 内核虚拟地址

内核页表对 `[KERNBASE, PHYSTOP)` RAM 以及 UART、VirtIO、PLIC 窗口采用恒等映射，所以这些物理地址可以直接作为内核指针使用。此外还建立两类高地址别名：

- `TRAMPOLINE` 映射到最高虚拟页，但物理页来自链接段 `trampsec`；
- 每个进程的内核栈映射在高地址 `KSTACK(i)`，相邻栈之间留一个未映射 guard page。

所有 CPU 共享 `kernel_pagetable`，但每个 CPU 单独写自己的 `satp` 并刷新 TLB。`proc_mapstacks()` 在启动时为每个进程槽永久分配一页栈；槽位变回 `UNUSED` 时不会释放该页，后续占用同一槽位的进程复用它。

### 5.3 用户虚拟地址

一个完成 `exec` 的进程大致具有：

```text
0
| ELF text/data/bss
| guard page（无 PTE_U）
| USERSTACK 个用户栈页
| 后续 sbrk 扩展的逻辑空间
| ... 未使用 ...
TRAPFRAME   每进程独有物理页，无 PTE_U
TRAMPOLINE  所有进程映射同一代码页，无 PTE_U
MAXVA
```

`p->sz` 是用户低地址区域的逻辑上界，不表示其中每一页都有物理映射。这个 fork 的普通 `sbrk()` 使用 eager allocation，`sbrklazy()` 只抬高逻辑上界并留下未映射洞；负增长无论使用哪种策略都立即通过 `growproc()` 解除跨过的完整页，但新边界所在的部分页若原本已映射仍会保留，不能把 `p->sz` 当作硬件逐字节保护界限。

上图中的 “ELF text/data/bss” 只是逻辑示意。`kexec()` 按 program header 出现顺序调用 `uvmalloc(pagetable, sz, ph.vaddr + ph.memsz, ...)`：如果下一个 segment 起点高于当前 `sz`，gap 中从 `PGROUNDUP(sz)` 开始的新整页会被分配、清零，并取得这次扩展使用的 PTE 权限；旧 `sz` 所在部分页的剩余字节已经属于旧页，继续保留前一段权限。如果 segment 重叠或逆序，已有页也不会重新分配或重新设置权限，`loadseg()` 仍可覆盖其中的字节。loader 因而依赖本仓库 linker 产生按地址递增、互不重叠的常规布局，不能把示意图理解成内核逐段保留了精确的 ELF 空洞和权限边界。

## 6. 主要对象及所有权

| 对象 | 创建/取得 | 所有权或引用 | 结束条件 |
|---|---|---|---|
| 物理页 | `kalloc()` | 单一调用者，除非被页表/对象接管 | `kfree()` |
| `struct proc` 槽 | `allocproc()` | 进程表固定槽，`p->lock` 保护状态 | 分配失败时内部回滚；正常 `ZOMBIE` 由父进程的 `kwait()` 调用 `freeproc()` |
| 固定内核栈页 | `proc_mapstacks()` | 启动时按进程槽分配，由共享 `kernel_pagetable` 映射 | 不随进程释放；同槽复用至关机 |
| trapframe 页 | `allocproc()` | 当前进程；用户页表在 `TRAPFRAME` 处借用映射 | `freeproc()` 调用 `kfree()` |
| 用户页表 | `proc_pagetable()`/`kexec()` | 构造期间由调用者临时持有，提交后由进程持有；trampoline/trapframe 映射不拥有对应物理页 | `proc_freepagetable()` |
| `struct file` | `filealloc()` | 全局 `ref`；可被多个 fd/进程共享 | 最后一次 `fileclose()` |
| 内存 inode | `iget()/namei()` | `ip->ref`；磁盘链接数另由 `nlink` 表示 | `ref` 降到零后槽可复用；仅 `nlink == 0` 时由 `iput()` 截断并回收磁盘 inode |
| buffer | `bread()` | `refcnt` 防止静态槽被替换，sleeplock 保证独占内容访问 | `brelse()` 降低引用；`refcnt == 0` 只表示可复用，日志可用 `bpin()` 延长引用 |
| pipe 页 | `pipealloc()` | 读端和写端共同拥有 | 两端都关闭后 `kfree()` |
| VirtIO 描述符链 | `alloc3_desc()` | `vdisk_lock` 下的一次在途请求 | 中断标记完成并唤醒；请求者醒来后 `free_chain()` |

`initproc` 是进程回收规则的特例：其他孤儿会被重新托付给它，但它自己若进入 `kexit()`，内核会直接 `panic("init exiting")`。

## 7. 固定容量和耦合约束

`kernel/param.h` 的值不是彼此独立的调优项：

| 常量 | 当前值 | 影响 |
|---|---:|---|
| `NCPU` | 8 | `cpus[]`、早期栈和最大 hart id |
| `NPROC` | 64 | 进程槽、固定内核栈数量 |
| `NOFILE` | 16 | 每进程 fd 表 |
| `NFILE` | 100 | 全局 open-file descriptions |
| `NINODE` | 50 | 同时缓存的内存 inode 数量 |
| `NDEV` | 10 | `devsw[]` 的 major 编号范围检查接受 0..9；只有安装了 read/write handler 的槽（当前是 `CONSOLE=1`）真正可用 |
| `ROOTDEV` | 1 | 当前唯一根文件系统设备号；不等于 console major 或 PLIC IRQ |
| `MAXARG` | 32 | `exec` 内核 argv 槽数；系统调用路径必须在数组内保留结尾空指针，所以最多接收 31 个非空参数 |
| `MAXOPBLOCKS` | 10 | 单个 `begin_op()`/`end_op()` 日志操作区间的空间预算 |
| `LOGBLOCKS` | 30 | 内存 redo log 的 home-block 数组容量；`log_write()` 在 absorption 扫描前先检查满容量，实际调用序列不能把“最终不超过 30”简单当成总是安全 |
| `NBUF` | 30 | buffer cache；还必须容纳被日志 pin 的 block |
| `FSSIZE` | 2000 | `mkfs` 生成的块数 |
| `MAXPATH` | 128 | 系统调用导入路径的缓冲区大小；包括结尾 NUL，故可接受的路径正文最多 127 字节 |
| `USERSTACK` | 1 | `exec` 分配的用户栈页数，不含 guard page |

修改日志相关三个常量时，必须同时检查 `begin_op()` 的预留公式、`filewrite()` 的分批上限和提交阶段所需的临时 buffer；完整推导见[文件系统与日志资源上界](../kernel/resource-bounds.md)。修改 `NCPU` 时必须保证 QEMU 的 `CPUS` 不超过它。

## 8. 全局并发原则

1. 固定数组槽位通常由一把全局 spinlock 或每槽 spinlock 管理，查找可能是线性扫描。
2. `acquire()` 内部调用 `push_off()`，`release()` 内部调用 `pop_off()`；只有嵌套深度 `noff` 降到零时，才按最外层保存的 `intena` 恢复中断。该深度还可能包含锁外显式执行的 `push_off()`，不能简单等同于持锁数。
3. 可能等待磁盘、控制台、pipe 或条件变化的路径使用 `sleep(chan, lock)` 原子地“登记睡眠并释放条件锁”。
4. 线程不能在实际睡眠期间保留普通 spinlock；`sleep(chan, lock)` 会在登记睡眠后原子地释放作为条件锁传入的 spinlock，并在醒来后重新获取。inode 和 buffer 需要跨潜在阻塞操作保护内容，因此长临界区使用 sleeplock。
5. `wait_lock` 必须先于任何相关 `p->lock` 获取；日志、inode 和 buffer 又形成各自的层次约束，详见[同步与锁](../kernel/synchronization.md)。
6. `scheduler()` 持有目标 `p->lock` 跨越 `swtch()`；进程第一次进入 `forkret()` 时继承并释放它，之后每次通过 `sched()` 返回调度器前都必须重新持有它，且此时只能剩这一把 spinlock（`noff == 1`）。
7. 用户指针从不直接由系统调用代码解引用。`walkaddr()` 实际只要求 `PTE_V|PTE_U`，不会独立检查 `PTE_R` 或叶项类型；`copyout()` 随后额外要求 `PTE_W`。当前页表构造路径保证普通用户叶项带 `PTE_R`，所以这是一项构造不变量，不是 copy helper 自己完整执行的权限验证。`copyin/copyout` 遇到未映射页时会尝试 `vmfault()`，此时才以当前进程的 `p->sz` 限制 lazy 补页，而 `copyinstr()` 不会补页。

## 9. 初始化依赖

CPU 0 建立共享对象，其他 CPU 等待 `started`：

```text
console/printk
  -> physical allocator
  -> kernel page table and per-proc stacks
  -> proc table
  -> trap/PLIC
  -> buffer/inode/file tables
  -> VirtIO
  -> first proc slot + empty user page table + root cwd reference
  -> scheduler
  -> forkret in process context
  -> fsinit (log recovery + orphan reclaim)
  -> exec /init
```

文件系统初始化不能直接放在 `main()`：日志恢复可能触发磁盘 I/O 并睡眠，而调度器尚未运行时无法完成这种等待。这个 fork 的 `userinit()` 不嵌入或加载 `initcode`；它创建空用户地址空间并取得根目录引用，第一次被调度到 `forkret()` 后才执行 `fsinit()` 和 `kexec("/init", ...)`。此时 `namei("/")` 之所以能早于 `fsinit()` 调用，是因为路径没有待遍历分量：它只在已初始化的 inode table 中为 `(ROOTDEV, ROOTINO)` 建立引用，不读取 superblock 或根目录块。这个例外不能推广到 `namei("/init")` 等普通路径。

非零 hart 不重复上述共享初始化；观察到 `started != 0` 后，每个 hart 仍须依次执行自己的 `kvminithart()`、`trapinithart()` 和 `plicinithart()`，然后进入 `scheduler()`。

## 10. 教学实现的边界

- 只有一个文件系统设备和一个全局 superblock；没有 mount。
- 没有用户、权限、信号、网络、通用的可变大小内核堆分配器或每进程内核页表；`kalloc()` 只分配整页物理内存。
- 调度器和许多资源查找使用固定数组线性扫描。
- `fork` 完整复制已映射物理页，不实现 copy-on-write；lazy 未映射洞被保留为洞。
- 日志是物理 redo log，批量提交当前所有 outstanding `begin_op()`/`end_op()` 操作区间；区间与系统调用不是一一对应，也没有日志校验和或 barrier API。
- 设备模型固定为 QEMU `virt`、16550A UART、PLIC 和强制 non-legacy 的 VirtIO MMIO v2 block device；磁盘驱动使用 split virtqueue，其 DMA、字节序和 feature 协商边界见[存储栈](../kernel/storage-stack.md)。

这些限制是理解代码的前提，不应在文档中暗示系统具有更广泛的 POSIX 或硬件兼容性。

## 11. 验证建议

- 用 `make qemu CPUS=1` 和默认多核分别启动，观察初始化输出和 shell。
- 在 `main()`、`scheduler()`、`usertrap()`、`virtio_disk_rw()` 设置断点，记录当前 `tp`、`satp`、`sstatus` 和 `myproc()`。
- 在 QEMU 内运行 `usertests -q` 验证核心边界，再运行完整 `usertests` 和 `grind` 做压力验证。
