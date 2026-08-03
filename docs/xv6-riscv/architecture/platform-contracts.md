# 平台、ISA 与外部规范契约

本文集中说明当前仓库代码与外部规范之间的边界。源码事实仍由 `Makefile`、`kernel/start.c`、`kernel/riscv.h`、`kernel/memlayout.h`、`kernel/trampoline.S`、`kernel/kernelvec.S`、`kernel/plic.c`、`kernel/uart.c`、`kernel/virtio.h` 和 `kernel/virtio_disk.c` 决定；规范引用用于解释这些寄存器、二进制布局和设备协议为何成立，不能覆盖源码中更窄或不完整的实现。

## 1. 版本基线和可复现性边界

| 领域 | 本文采用的参考基线 | 重点章节/主题 | 当前仓库的依赖 |
|---|---|---|---|
| RISC-V unprivileged ISA | Volume I, version 20191213 | RV64I、M、A、F、D、C；内存顺序与 `FENCE` | `-march=rv64gc`、整数/原子/压缩指令 |
| RISC-V privileged ISA | Privileged Architecture v1.12（源码语义参考；工具链属性另记） | Chapter 3 Machine-Level ISA；Chapter 4 Supervisor-Level ISA；Sv39 address translation | M-to-S 启动、委托、CSR、PMP、trap、`satp`、PTE |
| Sstc | Sstc extension v1.0 | `stimecmp`、`menvcfg.STCE`、timer pending | `timerinit()` 直接给 S-mode 开放 timer compare |
| RISC-V psABI | `draft-20251013-104b7dc3fd4bc8900d571905c1902dbb5e3d8a17` | Calling Convention；ELF Object Files；DWARF | `a0..a7`、callee-saved register、16-byte `sp`、ELF relocation/debug |
| ELF | System V ABI, Edition 4.1 | Chapter 4 Object Files；Chapter 5 Program Loading | `ELF64` header/program header、`PT_LOAD`、`p_filesz/p_memsz` 零填 |
| PLIC | RISC-V PLIC Specification v1.0.0 | priority、pending、enable、threshold、claim/complete | IRQ 1/10 路由到每 hart supervisor context |
| VirtIO | OASIS VirtIO 1.1 | 2.6 Split Virtqueues；4.2 VirtIO over MMIO；5.2 Block Device | modern MMIO v2、split ring、三 descriptor block request |
| UART | National Semiconductor PC16550D, June 1995 | Register Description、FIFO、Interrupt Identification、Line Status | 8-bit MMIO register、RHR/THR/IER/IIR/LSR、THRE（源码把 IIR 宏命名为 `ISR`） |
| QEMU | QEMU 8.2 `virt` machine documentation and `hw/riscv/virt.c`/`hw/riscv/boot.c` | direct kernel boot、memory map、`virtio-mmio`、`ns16550a`、PLIC | `-machine virt -bios none -kernel ... -m 128M -smp ...` |

规范入口：

- RISC-V ISA specifications: <https://riscv.org/technical/specifications/>
- RISC-V psABI（本文固定 revision）: <https://github.com/riscv-non-isa/riscv-elf-psabi-doc/tree/104b7dc3fd4bc8900d571905c1902dbb5e3d8a17>
- System V gABI: <https://www.sco.com/developers/gabi/>
- PLIC v1.0.0: <https://github.com/riscv/riscv-plic-spec/releases/tag/1.0.0>
- VirtIO 1.1: <https://docs.oasis-open.org/virtio/virtio/v1.1/virtio-v1.1.html>
- PC16550D data sheet: <https://www.ti.com/lit/ds/symlink/pc16550d.pdf>
- QEMU RISC-V `virt`: <https://www.qemu.org/docs/master/system/riscv/virt.html>

仓库只在 Makefile 中尝试检查 QEMU 版本：它截取 `major.minor` 后交给 `bc` 当十进制数与 7.2 比较，并不是语义版本比较；例如 `7.10` 会被当成 `7.1` 而误判。它也没有锁定 QEMU patch version、GCC、binutils、Perl、Python 或 GDB。本文记录的本地验证环境是 QEMU 8.2.2 和 `riscv64-linux-gnu-gcc` 13.3.0；换工具版本后必须重新执行本文第 11 节的移植检查。参考版本不是构建系统的自动约束。

一次已有 `kernel/kernel` 的离线样本还显示：`readelf -A` 报告 `Tag_RISCV_priv_spec: 1.11` 和 `Tag_RISCV_stack_align: 16-bytes`，`readelf -h` 的 ELF Flags 报告 `double-float ABI`，而 `gcc -Q --help=target -march=rv64gc` 显示工具链默认 ABI 为 `lp64d`。这是当前工具链写入对象属性的事实，不是 Makefile 锁定了 Privileged v1.11，也不是目标硬件能力的探测结果；本文仍以 v1.12 解释通用 CSR/Sv39 语义，并单独引用 Sstc v1.0。由于 Sstc 是通过内联的原始 CSR 编码使用的，当前 ELF `Tag_RISCV_arch` 不会替内核声明“实现了 Sstc”，不能用 `readelf -A` 代替运行时能力检查。

## 2. `rv64gc` 与内核实际保存的机器状态

`-march=rv64gc` 选择 RV64 基础整数 ISA 加通用扩展集合，包含 M/A/F/D/C；现代 GCC 展开属性还会列出 `Zicsr` 和 `Zifencei`（本地样本为 `rv64imafdc_zicsr_zifencei`）。CSR 指令的编码属于 Zicsr，具体 CSR 的权限和字段则由 privileged ISA 定义。当前汇编实际依赖：

- RV64I 的 64 位整数寄存器和 load/store；
- Zicsr 的 CSR 指令，以及 privileged ISA 定义的 M/S CSR；
- A 扩展的 `amoswap.w.aq/rl` 实现 spinlock；
- C 扩展可由编译器用于压缩指令；
- M 扩展可被编译器用于乘除。

但 `kernel/swtch.S` 只保存 `ra`、`sp` 和整数 callee-saved `s0..s11`，trapframe/kernelvec 也只保存整数寄存器。FS 是 `mstatus` 中经 `sstatus` 视图暴露的浮点状态字段；代码既不把 FS/VS 显式规范化为 Off，也不启用、保存或恢复 floating-point/vector state，更没有 lazy FP/vector context。当前 QEMU reset handoff 下这些状态不可用；若换固件/平台留下非 Off 的 FS/VS，用户或内核一旦执行相关指令，寄存器状态就可能跨进程泄漏或破坏。虽然 `rv64gc` 在编译目标中包含 F/D，当前内核支持契约仍是“不允许用户或内核执行依赖持久 FP 状态的代码”。新增浮点/vector 代码前必须定义启用位、trap 行为和每进程 context；仅扩大 `struct context` 还不够。

Makefile 没有显式 `-mabi`，实际 ABI 来自所选交叉 GCC 的工具链配置默认值，并且必须与 `-march` 相容；它不是由 `-march` 自动推导出的独立结论。当前工具链默认是 `lp64d`，内核 ELF flags 也标为 double-float ABI。当前 C 接口只使用整数/指针参数，因而没有跨边界传递浮点参数，但这不等于内核已经支持浮点指令或浮点上下文。可复现构建更稳妥的做法是显式记录工具输出和 ABI。可用：

```sh
riscv64-linux-gnu-gcc -Q --help=target -march=rv64gc | rg 'mabi|march'
readelf -h kernel/kernel
readelf -A kernel/kernel
```

## 3. M-mode 到 S-mode 的特权契约

`kernel/start.c:start()` 对应 privileged v1.12 的 Machine-Level CSR 和 trap delegation 规则：

1. `mstatus.MPP=S` 和 `mepc=main` 决定 `mret` 的目标特权级与 PC；
2. `satp=0` 使初始 S-mode 仍使用 bare physical addressing；
3. `medeleg/mideleg` 尝试把低 16 个实现支持的 cause 委托给 S-mode；CSR 是 WARL，写入 1 不保证读回仍为 1；
4. `sie.SEIE/STIE` 打开 S-mode 外部/时钟类别；`sstatus.SIE` 只在当前正处于 S-mode 时作为全局门；
5. `pmpcfg0` 的 entry 0 被配置为 TOR，给 S-mode 物理地址读、写、执行权限；
6. Sstc 的 `menvcfg.STCE` 控制 S-mode 对 `stimecmp` 的访问及比较功能；`mcounteren.TM` 独立控制 S-mode 读取 `time`。`clockintr()` 的 `w_stimecmp(r_time()+1000000)` 同时需要两者，但 TM 不授予 `stimecmp` 权限，STCE 也不授予 `time` 读取权限。

`pmpcfg0=0xf` 的低配置字节可逐位解码为 `R=W=X=1`、`A=01 (TOR)`、`L=0`；同一 CSR 中 entries 1 到 7 的配置字节被写成零，即 OFF。代码没有配置实现可能提供的更高编号 PMP entry。entry 0 没有前一项，所以 TOR 下界是 0，上界是实现实际接受的 `pmpaddr0 << 2`。源码写入 `pmpaddr0=0x3fffffffffffff`，名义上把上界推到 `2^56-4`，但 `pmpaddr0` 是 WARL，未实现的高地址位可以读回为零；因此严格结论只是它覆盖当前实现所需的低物理地址窗口，不能把这个常量解读成对任意物理地址宽度的“全部内存”承诺。`L=0` 还表示 entry 未锁定；当前代码在 `mret` 后不再回到 M-mode 修改它。

代码没有读回验证委托、PMP 或 Sstc CSR，也没有 SBI/CLINT timer fallback。平台若不实现 Sstc，`w_stimecmp()` 可产生 illegal instruction；若 `medeleg` 不接受 xv6 依赖的 U-mode ecall/page-fault 位，这些同步异常仍以 M-mode 为目标，而 xv6 没有 M-mode trap vector。`mideleg` 的 SEI/STI 位若不能置位，`sie` 作为被委托 `mie` 位的 S-mode 视图也无法真正启用对应分类，结果更可能是 S-mode 根本收不到中断，不能笼统描述成“中断会进入 M-mode”。本地 QEMU 8.2.2 的默认 `virt` 配置满足这些前提；Makefile 名义上的 7.2 十进制版本检查本身不能证明 CPU extension、CSR 或 delegation 能力，这也不是任意 RV64GC 硬件保证。

每个 hart 都把下一次 compare 写成 `time + 1000000`。源码注释中的“约 0.1 秒”依赖当前 QEMU `virt` 的 10 MHz timebase；内核不解析 device tree 的 `timebase-frequency`，换平台后同一 tick 增量可能代表完全不同的实际时间。

`mideleg=0xffff` 也不能解释成“委托所有未来中断”：它只尝试设置低 16 位，且实现可以拒绝保留给 M-mode 的 cause。审阅时必须按具体 `scause` 位检查，而不是把写入值当作协商结果。

## 4. Sv39 页表契约

Sv39 来自 privileged v1.12 的 address-translation 章节。当前实现选择：

- 4 KiB level-0 leaf，三级页表，每级 512 个 64 位 PTE；
- `satp.MODE=8`、ASID 0；
- 不支持 level-1/2 superpage leaf；
- `MAXVA` 只使用低 canonical half；
- trampoline/trapframe 使用 supervisor-only 高地址映射；
- 显式切换 kernel/user `satp` 的路径做本 hart 全量 `sfence.vma`；没有跨 hart shootdown，动态修改或撤销用户映射也没有通用的跨 hart TLB 失效协议。

叶 PTE 的 R/W/X 合法组合和 `PTE_U` 权限由硬件执行。当前代码创建映射时不预置 Accessed/Dirty，且没有 A/D software-fault handler，因此依赖平台硬件更新 A/D；完整后果见[虚拟内存](../kernel/memory.md)。

`sfence.vma` 是地址翻译同步，不是普通数据 cache flush，也不保证 VirtIO DMA 可见性。反过来，virtqueue 的 `__atomic_thread_fence(__ATOMIC_SEQ_CST)` 不能替代修改 PTE 后的 TLB 同步。内核把 UART、VirtIO 和 PLIC 以普通 `PTE_R|PTE_W` 恒等映射建立，未显式设置 PBMT/cache 属性；这依赖 QEMU/目标平台的 PMA 将这些物理区识别为设备内存，并不构成可移植的 cacheability 或 I/O-ordering 声明。

`sfence.vma` 也不等价于 `fence.i`。当前 `kexec()` 先由数据 store 填充新代码页，`uvmcopy()` 还会复制可能可执行的页，但仓库没有任何 `fence.i` 或跨 hart instruction-cache 同步协议；进程又可能迁移到另一个 hart 执行。当前 QEMU 行为使这些路径可用，不能据此宣称适用于一般的非一致 I-cache 实现。移植到真实硬件时，必须在新指令对本 hart可取指前执行适当 `FENCE.I`，并为可能执行该地址空间的其他 hart 定义远端同步/调度协议。

## 5. psABI、系统调用 ABI 与汇编边界

RISC-V psABI Calling Convention 规定整数参数使用 `a0..a7`，整数/指针返回使用 `a0`，双寄存器返回还会使用 `a1`；`s0..s11` 为 callee-saved，`ra` 为 caller-saved return address。标准 ABI 要求过程入口以及过程执行期间 `sp` 保持 16 字节对齐。当前代码据此建立三层协议：

| 边界 | 使用的 ABI 事实 | 仓库实现 |
|---|---|---|
| C -> user syscall stub | 前六个参数已在 `a0..a5` | `user/usys.S` 只设置 `a7`、`ecall`、`ret` |
| trap -> C handler | C callee 需要完整可恢复调用现场 | trampoline 保存用户的全部 GPR；`kernelvec` 保存 caller-saved GPR 加 `gp`，依靠 C ABI 保留 `s*`/恢复 `sp`，并有意保持当前 hart 的 `tp` 不变 |
| scheduler context switch | `swtch()` 还必须保存恢复点 `ra`；普通 caller-saved GPR 可丢弃 | `swtch.S` 保存 `ra/sp/s0..s11` |

`gp` 是这张表之外必须单列的工具链契约。`kernelvec` 保存/恢复被打断内核流已有的 `gp`，却不会创建一个正确值；更关键的是，`entry.S` 从未初始化 kernel `gp`，`uservec` 保存用户 `gp` 后也没有在跳入 C 前替换它。当前本地 GCC 在这些目标选项下报告 `-msmall-data-limit=0`，现有 `kernel/kernel` 反汇编中只有 `kernelvec` 和 trampoline 的显式 `gp` 保存/恢复，没有 C 代码的 `gp` 相对访问，所以当前产物可运行。Makefile 并未显式锁定该限制，不能把这个观察推广到任意工具链。升级编译器、链接器或 flags 时必须重新反汇编审计；可移植修复应显式禁止这类访问，或在所有进入内核 C 的入口正确建立 kernel `gp`。

psABI 不规定 system-call ABI。Linux 也选择用 `a7` 放系统调用号，但 xv6 的 syscall number、参数上限、返回/错误语义和 dispatcher 都是自己的私有协议，不能因寄存器相同就套用 Linux ABI。`exec` 新栈上的 `argc/argv` 由内核与 `user/ulib.c:start()` 私下约定，也不是由 ELF loader 自动构造的完整 System V initial process stack；没有 `envp`、auxv 或 dynamic linker 信息。

## 6. ELF 装载契约

QEMU `-kernel` 和 xv6 `kexec()` 都使用 ELF program header，但信任范围不同：

- QEMU 装载内核 `PT_LOAD`，在当前链接产物中把 `p_paddr == p_vaddr` 的 file bytes 放到对应 RAM，并把 `[p_filesz,p_memsz)` 清零；xv6 自己没有清内核 BSS。不要把这个当前产物事实推广成所有 ELF 的 `p_paddr`、`p_vaddr` 或 QEMU loader 选址规则。
- `kexec()` 读取用户 ELF header/program headers，把 `PT_LOAD` 复制到新页表，物理页初始清零自然形成用户 BSS。
- `ENTRY(_entry)` 只设置 ELF `e_entry`。当前 QEMU `virt -bios none` MROM stub 的直接交接目标是固定 DRAM 基址 `0x80000000`；因此链接还必须让 `_entry` 实际位于该地址。三者（MROM 目标、实际 `_entry`、ELF `e_entry`）数值相同是当前构建的协调结果，不是 `ENTRY` 单独造成的。

用户 loader 只校验 magic、`memsz >= filesz`、地址加法不溢出、页对齐以及装载/复制是否成功；它不验证 `elf` 的 class、endianness、type、machine、ABI/version、`phentsize/phoff/phnum` 范围，`ph.off + filesz` 溢出、segment overlap、`paddr` 或 entry 是否落在可执行段。它还按本地 `struct proghdr` 大小步进，而不是先确认输入声明的 `phentsize`。这适用于本构建产生的 ELF，不是敌对二进制解析边界；详见[`exec`](../kernel/exec.md)。

## 7. QEMU `virt` 机器契约

`kernel/memlayout.h` 的地址来自 QEMU `virt` 实现而非 RISC-V ISA：

```text
0x00001000  MROM reset vector
0x02000000  CLINT/ACLINT timer window（当前 Sstc 路径不访问）
0x0c000000  PLIC
0x10000000  ns16550a UART, IRQ 10
0x10001000  first virtio-mmio transport, IRQ 1
0x80000000  DRAM base / direct kernel handoff
```

Makefile 的 `-m 128M` 必须与 `PHYSTOP=KERNBASE+128MiB` 一致。减少 VM RAM 会让 allocator 管理不存在的物理页；增加 RAM 不会自动扩大 xv6 页池。`-smp CPUS` 还必须满足 `CPUS<=NCPU`，且依赖 QEMU 提供从 0 开始的稠密 hart ID；`_entry` 在选择 `stack0[hartid]` 前只读取 `mhartid`，既不做范围检查也不把稀疏 ID 映射为 CPU 索引。Makefile 还显式传入 `-global virtio-mmio.force-legacy=false`，因此 VirtIO transport 必须报告 modern MMIO version 2；删除或覆盖这个选项会落入驱动明确拒绝的 legacy 路径。

`-bios none` 取消 OpenSBI，不取消 QEMU 自带 MROM reset stub。xv6 不消费 QEMU 通常放在 `a0/a1` 的 hartid/device-tree 参数，而是自行读取 `mhartid` 并使用编译期地址；更换 board、固件启动方式或设备布局时必须重写启动和发现协议。`-drive file=fs.img,if=none,format=raw,id=x0` 与 `virtio-blk-device` 还把宿主文件以可写 raw backend 接入；Makefile 没有声明 cache、flush 或 snapshot 模式，故设备完成不等于真实掉电持久化。

Makefile 没有显式 `-cpu` 或 `aia=`。因此它依赖该 QEMU 版本 `virt` 的默认 CPU 能执行最终 ELF 实际发出的整数、乘除、原子、压缩、CSR/特权指令并提供 Sstc，同时依赖默认 PLIC 模式；F/D 虽在编译目标和 ABI 属性中出现，却不能按第 2 节所述当成可用的内核/用户 FP context。切换到缺少实际所需扩展的 CPU，或启用 AIA/IMSIC 取代 PLIC，都会越过当前启动、trap 和设备地址契约。迁移时应把 `-cpu`、`-machine ... aia=none`（若要固定 PLIC）和实际设备树/帮助输出记录下来，而不能只记录 `-machine virt`。

## 8. PLIC v1.0.0 契约

PLIC 要向某个 context 发 external notification，四层状态必须同时成立：source priority 非零、source pending、该 context enable、priority 严格大于 threshold。claim 读取本身可以随时执行且不受 threshold 限制；它在该 context 的 enabled pending source 中原子选择最高优先级 IRQ、清相应 pending，若没有则返回 0。handler 完成设备级确认后，再把同一非零 IRQ 写回该 hart 同一 context 的 claim/complete register。PLIC 不保证替软件核对 completion 是否正好匹配上次 claim，因此 IRQ 和 context 的配对仍是驱动协议责任。

当前宏硬编码 QEMU `virt` supervisor context offset（`context = 2*hart + 1` 的 enable/threshold/claim 地址公式）：

- 只写第一个 32-bit enable word；
- 把 IRQ 1 和 10 enable 到所有 hart；
- threshold 恒为 0、priority 恒为 1；
- 不实现 affinity、priority policy 或动态 source registration。

enable bitmap 只覆盖第一个 32-bit word，因此 IRQ 号大于等于 32 的 source 即使在 PLIC 中存在也不会被当前代码打开。PLIC MMIO 访问也没有显式的 I/O fence；当前顺序依赖 QEMU 的设备内存属性和 trap/锁边界，移植到弱排序或非一致平台时必须重新证明 claim、设备 ACK 与 complete 的顺序。

同一 source 可以向多个 eligible context 发 notification，但只有一次 claim 成功；因此 external trap 中 claim 0 是可处理边界。漏设备 ACK 会让当前 level/状态型 source 条件持续，漏 PLIC complete 会让 gateway 无法再次转交该请求。`devintr()` 对未知非零 IRQ 仅打印后 complete，故把 PLIC source 表视为可信配置；它不做设备寄存器的通用探测。

## 9. 16550A UART 契约

`kernel/uart.c` 使用 16550 compatible 的 byte-wide register model。DLAB 改变 offset 0/1 的含义；初始化写入 divisor 3，再恢复 8-bit word mode，并打开 FIFO 与 RX/TX interrupt。源码注释把 divisor 3 称为 38.4K，但驱动不读取输入时钟，实际 baud 由平台时钟和 UART 模型决定，不能从常数 3 单独推出。源码把 offset 2 的读寄存器宏命名为 `ISR`，但 16550A 读语义是 Interrupt Identification Register（IIR）；不能按传统“status register”解读。LSR bit 0 表示 RHR 可读，bit 5 表示 THR 能接受下一字节（THRE），不表示整个字符已经离开发送 shift register。

发送协议因此是：进程写一字节到 THR 后置 `tx_busy=1`，UART handler 观察到 THRE 时清 busy 并唤醒等待者（不要求该次中断只由 THRE 原因触发）。它没有软件 TX ring、timeout、flow control 或 termios。`uartputc_sync()` 轮询 THRE，可在中断/早期启动/panic 输出使用，但同步输出和普通发送没有统一跨 CPU 队列，消息顺序不受设备规范额外保证。

QEMU 提供的是兼容模型；代码没有读取 IIR FIFO capability bits、scratch register 或设备 ID 做 probe，也不处理 line-status/modem-status 错误。连接真实 UART 前必须确认 register stride、clock/divisor、endianness、IRQ polarity、MMIO I/O ordering 和 PLIC route。

## 10. VirtIO 1.1 契约与实现差距

当前设备参数强制 non-legacy MMIO version 2。三块 DMA 区域分别是 descriptor table、available ring 和 used ring；RISC-V 目标与 QEMU 都按 little-endian 工作，驱动直接读写结构体而没有 endian conversion。发布 `avail->idx` 前、通知设备前以及确认中断后读取 used ring 前，代码用 sequentially-consistent fence 交接内存所有权；这不是对任意非一致 DMA/cache 的通用同步方案。block request 使用 512-byte sector，而 xv6 `BSIZE=1024`，所以一个文件系统块对应两个 sector。

实现比 VirtIO 1.1 窄，并存在明确兼容性限制：

- `kernel/virtio.h` 没有定义 `DeviceFeaturesSel`/`DriverFeaturesSel`，驱动在读写 feature word 前也从不写 selector；这已经遗漏 VirtIO MMIO 4.2.2.2 对 selector 写入的 MUST 要求。当前 QEMU 的 reset selector 为 0，所以这组运行环境落在低 32 位；规范没有提供可移植的默认 selector，不能把同一结果推广到一般设备。代码也没有显式选择高 32 位并协商 `VIRTIO_F_VERSION_1`（bit 32），同时违反“设备必须 offer、驱动若看到必须 accept”这一保留特性规则；合规设备可以因此拒绝继续工作；
- probe 还硬性要求 QEMU vendor ID `0x554d4551`，所以即使另一厂商的 MMIO v2 block device 满足 VirtIO 1.1，也会被当前代码直接 panic；
- 在当前 QEMU selector 0 前提下取得的低 32 位中，代码只清除了 indirect descriptor、event index、multiqueue、read-only、SCSI 和 writeback-config 等明确列出的位；它没有清除 `VIRTIO_BLK_F_FLUSH`，也没有对其余设备特性做白名单交集。因此“驱动不发 FLUSH”不等于“驱动拒绝了 FLUSH 特性”，更不能把未知低位当作已实现；
- reset 后立即写 `ACKNOWLEDGE`，没有等待 `DeviceStatus` 读回 0。VirtIO 1.1 初始化序列要求驱动写 0 后等待读值归零，再设置 `ACKNOWLEDGE`；因此当前代码是协议违规，不只是缺少诊断。当前 QEMU 同步完成 reset 的行为掩盖了这一差距，不能推广到允许异步 reset completion 的设备；
- queue size 固定 8，一次请求占 3 个 descriptor，所以最多两笔同时 in flight；
- 驱动假定内核虚拟地址恒等于 DMA 物理地址，没有 IOMMU/cache-coherency API；
- 请求 status 任意非零都 panic，没有重试、设备 reset、FAILED status 或 I/O error 上送；used-ring 的 `id`、`len`、descriptor chain 和 interrupt status 也没有做范围/一致性校验，因而把设备视为可信；
- 不读取 block capacity/config generation，不处理 config-change interrupt，也不使用 discard/write-zeroes 等可选操作；日志不发送 `VIRTIO_BLK_T_FLUSH`。写请求完成只表示当前 backend 接受了请求，不提供真实掉电持久化保证；现有 crash 结论只适用于当前 QEMU process-kill/教学镜像模型。

精确 offset、对齐、producer/consumer 和回绕规则见[存储栈](../kernel/storage-stack.md)。

## 11. 移植或升级检查表

1. 记录 QEMU、GCC、binutils 和 GDB 的完整版本，不只记录 major/minor。
2. 用 `readelf -h -l -A` 核对 ELF class、machine、entry、`PT_LOAD` 和 arch attributes；用 `nm -n` 独立核对 `_entry` 地址。
3. 在目标 hart 读回 `misa`、delegation、PMP、`menvcfg`、`mcounteren` 和 `satp` 能力；当前写后不读的做法只适合已知 QEMU。
4. 明确 A/D 是硬件更新还是软件 fault，并验证 `sfence.vma`/shootdown 策略。
5. 对 board 的 RAM、PLIC context、UART stride/clock、VirtIO transport/IRQ 做设备发现或编译期断言。
6. 用 `readelf -A` 读取 `Tag_RISCV_priv_spec`/`Tag_RISCV_arch`，用 `readelf -h` 读取 ELF float ABI flags；不要把这些静态属性当成运行时 Sstc、PMP、A/D 或 PLIC 能力证明。
7. 查询编译器的 `-msmall-data-limit`，并用 `objdump -d kernel/kernel | rg '\bgp\b'` 审计最终指令；在入口没有建立 kernel `gp` 的当前实现中，任何 C 生成的 `gp` 相对访问都是移植阻断项。
8. 对 VirtIO 逐项验证 reset/status、feature selector（低/高 32 位）、`VIRTIO_F_VERSION_1`、queue layout、DMA cacheability、used-ring 边界和 config-change/error 路径。
9. 若启用 FP/vector、多线程、ASID、IOMMU 或非一致 DMA，补齐 context、同步、ownership 和错误恢复，不得只打开 ISA/feature 位。
10. 重新运行静态验证和单/多 hart 行为测试；崩溃测试还必须区分 QEMU process kill 与真实设备掉电模型。

规范描述允许的行为通常比 xv6 实现更广。本仓库文档中的保证必须落在“规范允许”与“当前代码实际处理”的交集，不能因为 QEMU 接受一次配置就推断所有合规硬件都会接受。
