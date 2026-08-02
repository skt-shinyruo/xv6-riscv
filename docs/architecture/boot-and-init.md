# 启动与初始化

本文描述当前仓库从 QEMU 进入 `_entry`，到各 hart 分别进入 `scheduler()`、首进程加载 `/init`，再到 shell 输出 `$ ` 提示符的完整启动协议。这里没有“所有 hart 都已进入调度器，首进程才能运行”的全局屏障。

## 1. 源码地图

- `kernel/kernel.ld`：固定内核加载地址、入口、trampoline 页和 `end`；
- `kernel/entry.S`：为每个 hart 选择早期栈并进入 C；
- `kernel/start.c`：machine mode 配置、Sstc 定时器和 `mret`；
- `kernel/main.c`：CPU 0 全局初始化、其他 CPU 汇合和调度器入口；
- `kernel/vm.c`：共享内核页表、固定进程栈映射和每 hart 的 `satp` 切换；
- `kernel/riscv.h`：启动使用的 CSR、位定义、`satp` 和中断辅助函数；
- `kernel/proc.c:userinit()`、`forkret()`：首进程的延后文件系统初始化和 `/init` 装载；
- `kernel/fs.c`、`kernel/log.c`、`kernel/exec.c`：恢复文件系统并从磁盘替换首进程映像；
- `kernel/trap.c`、`kernel/trampoline.S`：首进程首次返回用户态所需的 CSR、trapframe 和页表切换；
- `user/init.c`、`user/sh.c`：建立 console fd、启动 shell 并输出第一个命令提示符。

构建和 QEMU 参数见[构建、链接与文件系统镜像](../build/build-link-and-fs-image.md)，断点和寄存器观察方法见[VSCode/GDB 调试](../build/vscode-debug.md)。

### 1.1 启动链总览

先用一条主线定位各阶段，再进入后文看每一步的不变量：

```text
QEMU MROM reset stub (0x1000)
  -> fixed handoff address 0x80000000
  -> _entry: choose this hart's early stack
  -> start: configure M-mode state and execute mret
  -> main: global/per-hart initialization
  -> scheduler: choose the initial RUNNABLE process
  -> forkret: fsinit + kexec("/init")
  -> prepare_return/userret: switch to the user page table
  -> /init: open console, fork, exec("sh")
  -> sh: write fd 2, "$ "
```

这条链跨越 QEMU 固件替身、machine/supervisor/user 三个特权级、两个页表和一次上下文切换。阅读时应始终同时追踪当前 PC、栈、页表、特权级和运行实体，不能把它当成普通 C 函数的连续调用栈。

## 2. 链接布局与 QEMU 交接共同决定第一条 xv6 指令

`kernel/kernel.ld` 声明 `ENTRY(_entry)`，并把位置计数器设为 `0x80000000`。当前生成的内核中 `_entry` 正好位于 QEMU `-kernel` 的 xv6 交接地址。hart 复位后会先执行 QEMU `virt` 机器在 `0x1000` 提供的短 MROM reset stub；在当前 `virt -bios none` 直接启动路径中，stub 的跳转目标是 DRAM/固件起始地址 `0x80000000`，不是内核 ELF 的 `e_entry`。因此 `_entry` 是第一条 xv6 指令，而不是硬件复位 PC，但前提是链接布局确实把它放在这个固定交接地址。

这里有一个容易被链接脚本文本掩盖的脆弱点：`.text` 开头写着 `kernel/entry.o(_entry)`，但当前 `entry.o` 的输入 section 名是 `.text`，不是 `_entry`，所以该选择式本身匹配不到 section。实际布局还依赖 `Makefile` 将 `entry.o` 放在 `OBJS` 第一项，随后 `*(.text .text.*)` 按输入顺序收集它。`ENTRY(_entry)` 只设置 ELF header 的入口地址，并不会把符号自动搬到基址，也不会改变 QEMU MROM 中的跳转目标；若调整对象顺序或 section 命名，必须用 `nm -n` 重新确认 `_entry == 0x80000000`，并用 `readelf -h -l` 分别核对 ELF entry 与装载段。

当前 ELF 的 `PT_LOAD` 覆盖 `.text`、静态数据和 `SHT_NOBITS` 的 `.bss`，且 `p_memsz > p_filesz`。QEMU ELF loader 必须在启动 hart 前把这段差额清零；xv6 没有在 `_entry` 中自行清 BSS。这个装载契约保证 `started`、进程/CPU 表等静态对象获得 C 语言要求的零初值。改成 raw binary 或自定义 loader 时，必须显式保留同样的零填行为。

链接脚本还建立三个关键边界：

- `_trampoline`：页对齐的 `trampsec` 起点；段尾也向页边界对齐，断言含填充后的跨度恰好为一页，也就是实际 trampoline 代码不得超过一页；
- `etext`：可执行只读映射的结束位置；
- `end`：内核静态数据结束，物理页分配器从其上方开始管理内存。

如果 `_trampoline` 超过一页，链接直接失败。这不是运行期检查，因为所有用户页表只为它预留一个固定虚拟页。

## 3. QEMU 交接条件

Makefile 使用的核心启动参数是：

```text
-machine virt -bios none -kernel kernel/kernel -m 128M -smp $(CPUS)
```

完整参数还用 `-global virtio-mmio.force-legacy=false` 强制现代 VirtIO MMIO 接口，并把 `fs.img` 连接为 block device；这与 `virtio_disk_init()` 要求 MMIO version 2 相互依赖。只有 `make qemu` 依赖名义上的 7.2 版本检查，`make qemu-gdb` 不依赖它；该检查又把 `major.minor` 当十进制数交给 `bc`，不是可靠的语义版本比较，也不能替代 Sstc 能力探测。

`-bios none` 取消的是外部固件/OpenSBI payload，并不会移除 QEMU 机器自身的 MROM reset stub。xv6 也不解析固件可能传入的设备树参数：`_entry` 立即改写 `a0/a1`，设备地址和 RAM 上界全部来自编译期的 `memlayout.h`。

本实现依赖的交接条件是：

1. 每个 hart 先从 QEMU MROM reset vector `0x1000` 执行，再被 stub 交给固定物理地址 `0x80000000` 的 `_entry`；这个目标不是由 ELF `e_entry` 决定；
2. 到达 `_entry` 时仍处于 machine mode；
3. `mhartid` 对活跃 hart 是从零开始、可用于索引早期栈的值；
4. 分页尚未启用或会在 `start()` 明确关闭；
5. RAM 与 `kernel/memlayout.h` 中 `KERNBASE..PHYSTOP` 一致。

`CPUS` 不得超过 `NCPU`。代码没有在 `_entry` 检查越界；过大的 hart id 会把 `sp` 设置到 `stack0` 之外。

## 4. `_entry`：建立最小 C 环境

`kernel/entry.S` 为每个 hart 计算：

```text
sp = stack0 + (mhartid + 1) * 4096
```

`stack0` 是 `kernel/start.c` 中按 16 字节对齐的 `4096 * NCPU` 静态数组。栈向低地址增长，因此 hart 0 使用第一段的顶端、hart 1 使用第二段顶端。此时尚未使用最终的 per-process kernel stack。

设置 `sp` 后直接 `call start`。如果 `start()` 意外返回，控制流落到 `spin` 无限循环；正常路径通过 `mret` 离开，绝不返回 `_entry`。

## 5. `start()`：从 machine 准备 supervisor

所有 hart 独立执行相同的 `start()`。关键步骤及顺序如下。

### 5.1 指定 `mret` 的目标和权限

`mstatus.MPP` 被设为 supervisor，`mepc` 被设为 `main()`。最后执行 `mret` 时，硬件同时完成模式切换和 PC 跳转。

编译使用 `-mcmodel=medany`，使内核在高地址加载时仍能生成合适的 PC-relative 引用。

### 5.2 暂时关闭地址翻译

`w_satp(0)` 保证进入 `main()` 时仍按物理地址运行。CPU 0 随后建立 `kernel_pagetable`；每个 hart 在 `kvminithart()` 中单独启用 Sv39。

### 5.3 委托异常和中断

`medeleg`、`mideleg` 写入 `0xffff`，将可委托的异常/中断交给 supervisor。`sie` 开启 supervisor external interrupt 和 supervisor timer interrupt 对应位。

写入委托寄存器并不等于立刻允许普通中断。源码在安装 `stvec` 前也没有显式清 `sstatus.SIE`；它依赖 QEMU 交接时该位为 0，并由 `mret` 原样带入 S-mode。当前第一次显式 `intr_on()` 在 `scheduler()` 中，发生在 `trapinithart()` 之后。换启动环境时必须把 `SIE=0` 作为入口前提验证，而不能把它误认为 `start()` 已主动建立的状态。

### 5.4 开放物理内存

PMP entry 0 被配置为覆盖宽范围并允许读写执行，使 supervisor 可以访问内核使用的物理内存和 MMIO。若缺少该设置，模式切换成功后也可能因 PMP 拒绝访问而立即 trap。

### 5.5 配置 Sstc 定时器

`timerinit()`：

1. 设置 `menvcfg` 的 STCE 位；
2. 设置 `mcounteren` 的 TM 位，使 S-mode 可以读取 `time`；`menvcfg.STCE` 单独控制 S-mode 对 `stimecmp` 的访问和比较功能，TM 不授予 `stimecmp` 权限；
3. 把首次 `stimecmp` 设为 `time + 1000000`。后续 S-mode 的 `clockintr()` 同时读取 `time` 并写 `stimecmp`，所以整条表达式需要 TM 与 STCE 两项权限，但两者作用不同。

后续每次 timer interrupt 都由 `clockintr()` 再次设置 `stimecmp`。这版实现不使用旧版 xv6 的 machine-mode timer scratch/软件中断转发方案。

### 5.6 固定 hart id

最后读取 `mhartid` 并写入 `tp`。进入用户态时用户程序可以改写自己的 `tp`，所以从用户 trap 回内核时，`uservec` 会从 trapframe 恢复内核保存的 hart id。

## 6. `mret` 后的初始状态

执行 `mret` 后：

| 状态 | 值 |
|---|---|
| 特权级 | supervisor |
| PC | `main()` |
| `sp` | 当前 hart 的 `stack0` 区域 |
| `tp` | 当前 hart id |
| `satp` | 0，未分页 |
| trap vector | 尚未安装 supervisor `stvec` |
| 进程 | 无，`cpus[id].proc` 尚未设置 |

因此 `main()` 在安装 trap vector 和 PLIC 之前不能依赖完整的中断处理环境。

## 7. CPU 0 的初始化顺序

`kernel/main.c` 让 CPU 0 执行共享初始化：

```text
consoleinit
printkinit
kinit
kvminit
kvminithart
procinit
trapinit
trapinithart
plicinit
plicinithart
binit
iinit
fileinit
virtio_disk_init
userinit
publish started = 1
```

顺序中的主要依赖是：

- 控制台先于启动日志；`printkinit` 随后才使并发格式化输出受锁保护；
- `kinit` 先于页表和 VirtIO 队列内存分配；
- `kvminit` 内部调用 `proc_mapstacks`，所以它先构造所有固定进程内核栈映射；
- `stack0` 位于内核映像的 RAM 恒等映射内，所以 `kvminithart` 启用分页时当前 `sp` 无需改变；此后 hart 才能访问高地址 kernel stack 别名。调度器第一次 `swtch()` 切到进程栈，进程返回调度器时又从 `cpu.context` 恢复该 hart 的 `stack0` 栈；
- `procinit` 初始化 `p->lock`、`wait_lock` 和每个 `kstack` 虚拟地址；
- `trapinithart` 和 `plicinithart` 都是 per-hart 操作；
- buffer、inode、file 表和 VirtIO 必须在文件系统被任何进程使用前完成；
- `userinit` 经 `allocproc()` 建立首进程、空用户页表和 trapframe，并用 `namei("/")` 取得当前目录的内存 inode 引用，最后将它置为 `RUNNABLE`。路径 `/` 没有待遍历的分量，所以这里的 `namei()` 只执行 `iget()`，不会 `ilock()` inode 或发起磁盘 I/O。

### 7.1 `kvmmake()` 实际建立的映射

| 虚拟范围 | 物理范围 | PTE 权限 | 说明 |
|---|---|---|---|
| `UART0..UART0+PGSIZE` | 恒等 | R/W | 16550A MMIO |
| `VIRTIO0..VIRTIO0+PGSIZE` | 恒等 | R/W | VirtIO MMIO transport |
| `PLIC..PLIC+0x4000000` | 恒等 | R/W | PLIC priority/pending/contexts 窗口 |
| `KERNBASE..etext` | 恒等 | R/X | 内核 text 与 trampoline 原始位置，不可写 |
| `etext..PHYSTOP` | 恒等 | R/W | 内核其余静态数据与可分配 RAM，不可执行 |
| `TRAMPOLINE` 一页 | `_trampoline` 物理页 | R/X | 同一代码页的高地址别名 |
| 每个 `KSTACK(i)` 一页 | 启动时分配的页 | R/W | 相邻低地址页不映射，作为 guard |

当前链接脚本把 `.rodata` 放在 `etext` 之后，所以它实际落入 R/W 映射，并没有硬件只读保护。分页前的内核 ELF 又是单个 RWE `PT_LOAD`；ELF segment flags、section 名称和最终 PTE 权限是三套不同事实。`stack0` 位于 `etext..PHYSTOP` 的 R/W 恒等映射中，各 hart 4 KiB slice 相邻且没有 guard/canary；它会继续作为 scheduler 栈，而不只是临时启动栈。

`mappages()` 创建叶 PTE 时不预置 A/D 位。若平台只支持以 page fault 交给软件设置 A/D 的 Svade 行为，`kvminithart()` 写入 `satp` 后的第一次内核取指就可能 fault；此时 `trapinithart()` 尚未执行，`stvec` 还没有有效 supervisor 向量，当前内核无法恢复。因而“硬件自动更新 A/D”是启用分页前的启动前提，而不是普通运行期降级路径。

## 8. 其他 hart 的汇合

非零 hart 在未分页的早期栈上轮询 `started`。CPU 0 完成共享初始化后执行顺序一致性 fence，再写 `started = 1`；其他 hart 观察到非零后也执行 fence，然后完成自己的：

```text
kvminithart -> trapinithart -> plicinithart -> scheduler
```

`volatile` 阻止编译器把轮询优化掉，两个 `__atomic_thread_fence(__ATOMIC_SEQ_CST)` 在当前 GCC/RISC-V 产物中形成 fence + 普通 store/load 的发布/观察序列。`started` 本身却不是 C11 `_Atomic`，并发普通读写在 ISO C 抽象机中仍是 data race；所以这只是当前工具链和目标机器层面的实现协议，不是可移植 C 原子发布。协议还假定只有 CPU 0 写，其他 CPU 只读；可移植加固应改用带 release/acquire 语义的原子对象。

非零 hart 不能重新执行 `kvminit()`、`procinit()` 等全局初始化，否则会替换共享页表或重置已经可见的锁/状态。

`started` 是“共享初始化已经发布”的单向门闩，不是等待所有 hart 的 barrier。CPU 0 写入 `started = 1` 后便可立即进入自己的 `scheduler()`；非零 hart 只是各自在观察到该值后完成 per-hart 初始化。于是 CPU 0 可能在其他 hart 尚未进入调度器、甚至尚未打印 `hart N starting` 时就调度首进程；也可能由一个较快的非零 hart 抢先取得唯一的 `RUNNABLE` 首进程。`p->lock` 使同一进程只会被一个 scheduler 选中，启动正确性不依赖所有 hart 同时到达。

## 9. 为什么 `main()` 不直接初始化文件系统

`fsinit()` 会：

1. 通过 `bread()` 读取 superblock；
2. `initlog()` 读取并可能安装未完成的已提交事务；
3. `ireclaim()` 扫描并回收 orphan inode。

这些步骤会向 VirtIO 发请求，然后在等待完成中断时调用 `sleep()`。在 `main()` 尚未进入调度器时睡眠，没有已经由 `swtch()` 保存好的调度器上下文可供切换。因此 `userinit()` 只准备空用户地址空间和根目录引用，把首进程置为 `RUNNABLE`，而不加载任何用户代码。

## 10. 首进程的特殊 `forkret()`

`allocproc()` 把新进程上下文的 `ra` 设为 `forkret`。调度器第一次 `swtch()` 到首进程时：

1. `forkret()` 继承调度器持有的 `p->lock`，先释放它；
2. 静态 `first` 为 1，调用 `fsinit(ROOTDEV)`；
3. 将 `first` 清零并执行 fence；
4. 调用 `kexec("/init", {"/init", 0})`，失败则 `panic("exec")`；
5. 成功时把返回的 `argc` 放入 trapframe `a0`；
6. `prepare_return()` 准备 CSR/trapframe；
7. 跳到 trampoline 中的 `userret`，切用户页表并 `sret`。

后续普通 `fork` 子进程第一次进入 `forkret()` 时跳过文件系统初始化和 `/init` 装载，因为它已经继承了父进程地址空间。

`first` 自身没有锁保护，它的正确性依赖一个更强的启动时序：初始进程返回用户态之前，系统中没有任何能调用 `fork` 并创建第二个进程的执行者。多个 CPU 虽然可能竞争这个唯一的 `RUNNABLE` 进程，但 `p->lock` 和状态转换保证只有一个 CPU 能进入它的首次 `forkret()`；该路径在进入用户态前完成 `fsinit()`、清零 `first` 并执行 fence。此后才可能产生普通 `fork` 子进程，它们首次进入 `forkret()` 时观察到 `first == 0`。不能把这个一次性启动协议当作通用的无锁初始化模式。

## 11. 从 `/init` 到 shell 提示符

`kexec()` 成功后，`prepare_return()` 配置用户返回所需的 `stvec`、`sstatus` 和 `sepc`，trampoline 的 `userret` 切换到首进程页表、恢复用户通用寄存器并执行 `sret`。常规用户 ELF 从 `user/ulib.c:start()` 进入，再调用 `/init` 的 `main()`。

`user/init.c` 随后完成用户空间最后一段启动：

1. 打开或创建设备节点 `console`，确保 fd 0 可用；
2. 用 `dup(0)` 建立 fd 1 和 fd 2，因此标准输入、输出和错误都指向 console；
3. `fork()` 一个 child，child 执行 `exec("sh", argv)`；
4. parent 反复 `wait()`，回收 shell 以及转交给 init 的孤儿；shell 退出后重新创建它。

`user/sh.c:getcmd()` 每次读命令前执行 `write(2, "$ ", 2)`。所以屏幕出现 `$ ` 不是单独的内核启动步骤，而是 QEMU 交接、内核初始化、调度、文件系统恢复、ELF 装载、trap 返回、console fd 建立和 shell 用户代码全部成功后的首个用户可见结果。`init` 的重启与孤儿回收细节见[`init` 与 shell](../user/init-and-shell.md)。

## 12. 失败模式

- 早期栈不足或 `CPUS > NCPU`：可能破坏静态数据，启动代码无恢复路径。
- `kalloc()` 无法分配进程栈或 VirtIO 队列页：相应初始化代码 `panic`。更早的内核根页表分配和 `userinit()` 对 `allocproc()` 的返回值没有完整判空，内存极端不足时可能直接 fault，而不是得到整洁的失败返回。
- VirtIO 标识、版本、队列容量或 feature negotiation 不符合预期：`virtio_disk_init()` `panic`。
- 文件系统 magic 错误、首个 `/init` 不存在或 ELF 无效：首进程启动 `panic`，不会回退到救援 shell。
- 某个非零 hart 永远看不到 `started`：它会持续忙等；在此阶段没有 watchdog。

## 13. 调试与验证

### 13.1 单核确认顺序

```sh
make qemu CPUS=1
```

预期看到启动信息、`init: starting sh` 和 `$ `。在 `_entry`、`start`、`main`、`userinit`、`forkret`、`kexec` 依次设断点，可确认首次控制流。

### 13.2 多核确认发布协议

使用默认 `CPUS=3`，在 `main` 的两条分支观察：只有 hart 0 执行共享初始化，其他 hart 在 `started` 后打印 `hart N starting` 并完成自己的初始化。不要把这些打印全部早于 `/init` 或 shell 当作正确性条件；发布后各 hart 与首进程并发推进，实际输出顺序由调度决定。

### 13.3 寄存器检查点

| 断点 | 应检查 |
|---|---|
| `_entry` | `mhartid`、物理 `sp` |
| `start` 末尾 | `mstatus.MPP`、`mepc`、`satp=0`、`tp` |
| `main` 中 `kvminithart` 后 | `satp` 的 Sv39 mode 和根页表 PPN |
| `forkret` | `myproc()`、`p->state`、当前 kernel stack |
| `userret` | 用户 `satp`、`sepc`、trapframe 中 `sp/a0/a1` |
