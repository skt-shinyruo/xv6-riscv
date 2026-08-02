# Trap、中断与用户态往返

本文说明当前仓库中 RISC-V trap 子系统的真实实现。这里的 **trap** 是异常（exception）和中断（interrupt）的统称：系统调用、缺页和非法指令是同步异常，时钟、UART 和 VirtIO 是异步中断。重点不是只列出处理函数，而是解释 CPU 进入 S-mode 后，代码怎样保存现场、切换栈和页表、分派原因，并在所有寄存器与控制状态寄存器（CSR）满足约束后返回用户态。

## 1. 文档边界与源码映射

本篇直接映射以下核心源码；这些路径也是审阅本文时必须逐一核对的实现依据：

| 源码路径 | 本文覆盖的内容 |
| --- | --- |
| `kernel/trap.c` | trap 初始化、`usertrap()`、`prepare_return()`、`kerneltrap()`、时钟与设备中断分派 |
| `kernel/trampoline.S` | 用户 trap 的汇编入口 `uservec` 与返回入口 `userret`，寄存器、页表和栈切换 |
| `kernel/kernelvec.S` | 内核 trap 汇编入口 `kernelvec`，内核栈上的寄存器现场 |
| `kernel/proc.h` | `struct trapframe`、`struct cpu` 与 `struct proc` 中的相关状态和偏移契约 |
| `kernel/plic.c` | PLIC 全局初始化、每 hart 初始化、claim/complete 协议 |
| `kernel/riscv.h` | `sstatus`、`sie`、`stvec`、`sepc`、`scause`、`stval`、`satp`、`stimecmp` 和中断开关封装 |

为解释上述路径的调用者、映射建立和可验证行为，本文还引用以下关联源码：

| 源码路径 | 关联点 |
| --- | --- |
| `kernel/start.c` | M-mode 向 S-mode 委托 trap，启用 S-mode 外部/时钟中断并首次设置 `stimecmp` |
| `kernel/main.c` | `trapinit()`、`trapinithart()`、`plicinit()`、`plicinithart()` 的启动顺序 |
| `kernel/memlayout.h` | `TRAMPOLINE`、`TRAPFRAME`、PLIC、UART、VirtIO 地址与 IRQ 编号 |
| `kernel/kernel.ld` | 将 `trampsec` 页对齐并限制为一页 |
| `kernel/vm.c` | trampoline 的内核映射、`vmfault()` 的延迟分配实现 |
| `kernel/proc.c` | 用户页表中的 trampoline/trapframe 映射、`forkret()` 首次返回、`yield()` 与睡眠唤醒 |
| `kernel/syscall.c` | 系统调用号和参数如何从 trapframe 读取，返回值如何写回 `a0` |
| `kernel/spinlock.c` | 自旋锁与 `push_off()`/`pop_off()` 的中断不变量 |
| `user/usys.pl` | `a7 = SYS_*; ecall; ret` 的用户系统调用桩生成规则 |
| `user/usertests.c` | 系统调用、抢占、非法地址与 lazy allocation 的回归测试 |

系统调用的具体分派表、进程调度和虚拟内存算法分别属于对应模块；本文只展开它们与 trap 边界相接的部分。

## 2. 硬件进入 trap 时做什么

以委托给 S-mode 的 trap 为例，RISC-V 硬件完成的关键动作是：

1. 把返回位置写入 `sepc`：同步异常通常指向引发异常的指令，异步中断指向被打断后应继续执行的位置。
2. 把原因写入 `scause`；若原因需要补充地址，则把地址写入 `stval`。
3. 把 trap 前的特权级写入 `sstatus.SPP`。
4. 把 trap 前的 `sstatus.SIE` 保存到 `sstatus.SPIE`，再清零 `SIE`，因此进入处理器时 S-mode 中断关闭。
5. 将当前特权级切到 S-mode，并按 `stvec` 的 BASE/MODE 选择入口 PC。

硬件**不会**自动切换页表，不会切换栈，也不会保存通用寄存器。由此产生两条完全不同的入口：

- 从 U-mode 进入时仍在使用用户页表和用户 `sp`，必须先进入同时映射在两个页表中的 `uservec`。
- 从 S-mode 进入时已经使用内核页表和内核栈，可以直接进入 `kernelvec`。

本实现写入 `stvec` 的 `kernelvec` 和 trampoline 中 `uservec` 地址都至少 4 字节对齐，低两位 MODE 为 0，因此使用 Direct 模式：所有异常和中断先进入同一个 BASE，再由 C 读取 `scause` 分派，而不是由硬件按 cause 选择向量槽。

`start()` 通过 `w_medeleg(0xffff)` 和 `w_mideleg(0xffff)` 尝试把低 16 个 cause 中平台支持的异常和中断委托给 S-mode，并在 `sie` 中打开 `SEIE`（外部中断）和 `STIE`（时钟中断）。`medeleg/mideleg` 是 WARL CSR，实现不支持的位可以保持为零；当前代码没有读回校验，因而依赖 QEMU 接受本实现用到的 SEI、STI、U-mode ecall 和 page-fault 委托位。

`sie` 的对应位决定某类 S-mode 中断是否启用；`sstatus.SIE` 只在 CPU 当前也运行于 S-mode 时充当 S-mode 全局开关。CPU 运行于更低的 U-mode 时，目标为 S-mode 的已启用中断在全局意义上视为开启，并不要求 `SIE == 1`。同步异常不依赖 `sie` 或 `SIE`。trap 一旦进入 S-mode，硬件清零 `SIE`，从而默认禁止同 hart 上的嵌套 S-mode 中断。

### 2.1 CSR 与投递门控矩阵

中断的“请求已产生”、“目标是 S-mode”、“该类中断已启用”和“当前允许打断 S-mode”是四个不同条件：

| cause | 请求/pending 的来源 | 到 S-mode 的路由 | cause 分类开关 | CSR 之前的上游门 |
| --- | --- | --- | --- | --- |
| supervisor external interrupt，code 9 | PLIC 对本 hart supervisor context 产生 notification，反映为 `sip.SEIP` pending | `mideleg.SEI=1` | `sie.SEIE=1` | 设备自身的 interrupt enable/ack，以及 PLIC priority、pending、context enable 和 threshold |
| supervisor timer interrupt，code 5 | Sstc 在 `time >= stimecmp` 时使 `sip.STIP` pending | `mideleg.STI=1` | `sie.STIE=1` | `menvcfg.STCE=1` 与 `mcounteren.TM=1` 共同开放 S-mode 的 `stimecmp`，TM 还开放 `time`；将 compare 写到未来值使当前请求解除并预约下次请求 |
| 来自 U-mode 的同步异常 | 当前指令执行直接产生，不经 pending 位 | `medeleg[cause]=1` | 无；不受 `sie` 控制 | 例如 `ecall`、页表翻译或权限检查 |

对前两类目标为 S-mode 的中断，当 pending、delegation 和对应 `sie` 位均满足后，当前特权级再决定 `SIE` 的作用：

| 当前特权级 | 是否能取目标为 S-mode 的中断 | `sstatus.SIE` |
| --- | --- | --- |
| M-mode | 不能在 M-mode 期间向低特权级的 S-mode 取 trap | 不能改变这一结果 |
| S-mode | 仅 `SIE=1` 时 | S-mode 全局中断门 |
| U-mode | 可以，不要求 `SIE=1` | 不参与这次投递判定 |

`start()` 设置 `sie.SEIE/STIE` 后，当前代码不再动态修改这两个分类位。名称较宽泛的 `intr_on()`/`intr_off()` 实际只置位/清零 `sstatus.SIE`，不会改写 `sie`、PLIC 或设备 IER；因而它们只是上表中“当前正在 S-mode”这一格的全局门操作。

`stvec` 不是投递资格的第五道门。硬件接受 trap 后才用 `stvec.BASE/MODE` 决定入口 PC；错误或不可取指的 BASE 会破坏处理路径，但不会反过来屏蔽请求。同样，`scause` 是接受后的结果，PLIC claim 是 external handler 进入后的取号操作，它们都不是上游投递门。

`trap.c` 不轮询或直接清除 `sip.SEIP/STIP`。CPU 接受后通过 `scause` 告知原因；external pending 由具体设备确认加 PLIC claim/complete 协议推进，timer pending 则由 `clockintr()` 把 `stimecmp` 推到未来值来解除。

### 2.2 首次安全投递的初始化边界

`start()` 在进入 `main()` 前就设置首个 `stimecmp`，所以 timer 可以在高层内核初始化完成前成为 pending。此时每个 hart 仍在 S-mode，`start()` 只设置 `sie.STIE/SEIE`、没有调用 `intr_on()`；当前实现依赖 QEMU/reset 状态使早期 `sstatus.SIE=0`，因而 pending 不会在安全入口就绪前打断 S-mode 启动代码。

boot hart 中的相关顺序是：

```text
trapinit()       -> 只执行 initlock(&tickslock, "time")
trapinithart()   -> 本 hart stvec <- kernelvec
plicinit()       -> 全局 UART/VirtIO source priority
plicinithart()   -> 本 hart enable bitmap 和 threshold
... virtio_disk_init(), userinit() ...
started = 1
scheduler()      -> 每轮短暂 intr_on()，随即 intr_off()
```

`trapinit()` 的名字和 `main.c` 中的“trap vectors”注释容易误导：当前函数不写任何 trap CSR，只初始化由 hart 0 更新 `ticks` 时使用的共享 `tickslock`；真正的向量安装是每 hart 调用的 `trapinithart()`。其他 hart 在 `started` 发布后才继续，此时共享 `tickslock` 已初始化，然后它们分别安装自己的 `stvec` 和 PLIC context，最后进入 scheduler。因此 scheduler 的首次 `intr_on()` 是对 S-mode pending 中断的第一个安全开放点；之后返回 U-mode 时，则如上表所示不再以 `SIE` 为投递门。

### 2.3 本实现识别的 `scause`

| `scause` | 含义 | 当前处理 |
| --- | --- | --- |
| `8` | U-mode environment call，即 `ecall` | 进入 `syscall()`；返回地址前移 4 字节 |
| `13` | load page fault | 尝试 lazy allocation；失败则杀死进程 |
| `15` | store/AMO page fault | 尝试 lazy allocation；失败则杀死进程 |
| `0x8000000000000005` | supervisor timer interrupt | `clockintr()`，返回设备类型 `2` |
| `0x8000000000000009` | supervisor external interrupt | 经 PLIC 分派，返回设备类型 `1` |
| 其他 | 未识别异常或中断 | 用户 trap 杀进程；内核 trap 直接 panic |

最高位为 1 表示异步中断，其余低位是 cause code。`stval` 在缺页时通常是出错虚拟地址，在非法指令等异常时由硬件按规范提供附加信息。代码不把 instruction page fault（cause 12）作为 lazy allocation，因此这类错误走未识别分支。

## 3. 两个固定高地址为何必要

`kernel/memlayout.h` 定义：

```c
#define TRAMPOLINE (MAXVA - PGSIZE)
#define TRAPFRAME  (TRAMPOLINE - PGSIZE)
```

每个用户页表都把同一份 `trampoline.S` 物理页映射到 `TRAMPOLINE`，权限是 `PTE_R | PTE_X`；内核页表也在同一虚拟地址映射同一物理页。两份映射都没有 `PTE_U`，所以用户程序不能直接读取或执行 trampoline，但 CPU 切到 S-mode 后可以在尚未更换的用户页表中取指。

每个进程还把自己的 `p->trapframe` 物理页映射到固定虚拟地址 `TRAPFRAME`，权限是 `PTE_R | PTE_W` 且没有 `PTE_U`。`uservec` 因而能在用户页表仍生效时保存现场，用户代码本身却不能访问该页。

内核页表没有在 `TRAPFRAME` 这个固定虚拟地址专门映射 trapframe。进入 C 代码后，内核通过 `p->trapframe` 对应的内核可访问地址读写同一物理页。这个区别决定了入口汇编的顺序：所有需要从 `TRAPFRAME` 读取的内核启动字段必须在写 `satp` 之前取完。

`kernel.ld` 把 `trampsec` 放在页边界，并用断言保证该段恰好占一个 4 KiB 页面。`stvec` 不能直接使用链接时 `uservec` 的普通地址，因为用户页表只在 `TRAMPOLINE` 映射它；`prepare_return()` 因此计算：

```text
TRAMPOLINE + (uservec - trampoline)
```

同理，`forkret()` 首次进入用户态时使用 `TRAMPOLINE + (userret - trampoline)` 调到返回入口。

## 4. `struct trapframe` 的二进制契约

`kernel/proc.h` 与 `kernel/trampoline.S` 通过硬编码字节偏移共享布局。结构共 36 个 `uint64`，大小为 288 字节。只要调整字段顺序或类型，就必须同步修改汇编偏移；否则不会得到类型错误，而会静默破坏用户寄存器或内核入口参数。

| 偏移 | 字段 | 入口/返回用途 |
| ---: | --- | --- |
| 0 | `kernel_satp` | `uservec` 读取并安装内核页表 |
| 8 | `kernel_sp` | `uservec` 装入 `sp`，指向当前进程内核栈顶 |
| 16 | `kernel_trap` | `uservec` 读取 `usertrap()` 地址并 `jalr` |
| 24 | `epc` | C 代码保存/恢复用户 PC；汇编不直接访问 |
| 32 | `kernel_hartid` | `uservec` 恢复内核约定的 `tp = hartid` |
| 40 | `ra` | 用户 `ra` |
| 48 | `sp` | 用户 `sp` |
| 56 | `gp` | 用户 `gp` |
| 64 | `tp` | 用户 `tp`；与内核 hart id 是不同值 |
| 72 | `t0` | 用户临时寄存器 |
| 80 | `t1` | 用户临时寄存器 |
| 88 | `t2` | 用户临时寄存器 |
| 96 | `s0` | 用户被调用者保存寄存器 |
| 104 | `s1` | 用户被调用者保存寄存器 |
| 112 | `a0` | 用户参数/系统调用返回值 |
| 120 | `a1` | 用户参数寄存器 |
| 128 | `a2` | 用户参数寄存器 |
| 136 | `a3` | 用户参数寄存器 |
| 144 | `a4` | 用户参数寄存器 |
| 152 | `a5` | 用户参数寄存器 |
| 160 | `a6` | 用户参数寄存器 |
| 168 | `a7` | 用户参数/系统调用号 |
| 176 | `s2` | 用户被调用者保存寄存器 |
| 184 | `s3` | 用户被调用者保存寄存器 |
| 192 | `s4` | 用户被调用者保存寄存器 |
| 200 | `s5` | 用户被调用者保存寄存器 |
| 208 | `s6` | 用户被调用者保存寄存器 |
| 216 | `s7` | 用户被调用者保存寄存器 |
| 224 | `s8` | 用户被调用者保存寄存器 |
| 232 | `s9` | 用户被调用者保存寄存器 |
| 240 | `s10` | 用户被调用者保存寄存器 |
| 248 | `s11` | 用户被调用者保存寄存器 |
| 256 | `t3` | 用户临时寄存器 |
| 264 | `t4` | 用户临时寄存器 |
| 272 | `t5` | 用户临时寄存器 |
| 280 | `t6` | 用户临时寄存器 |

`x0` 恒为零，无需保存。用户 PC 位于 `sepc`，由 C 保存到 `epc`。`a0` 是入口的特殊难点：汇编需要把它改成 `TRAPFRAME`，所以先执行 `csrw sscratch, a0`，保存其用户值；其他寄存器落盘后再从 `sscratch` 取回并写到偏移 112。返回时 `a0` 最后恢复，因为在此之前它仍作为 trapframe 基址。

偏移 0、8、16、32 的四个 `kernel_*` 字段不是用户现场，而是下一次从该进程进入内核所需的引导数据；`prepare_return()` 在每次返回用户态前刷新它们，不能依赖其跨调度永久不变。偏移 24 的 `epc` 则是用户控制现场：`usertrap()` 在入口保存它，系统调用路径将它前移 4 字节，`exec` 等建立新用户映像的路径也会设置它；`prepare_return()` 只读取 `epc` 写入 `sepc`，不会刷新其值。

## 5. 用户态 trap 入口：`uservec` 到 `usertrap()`

完整路径如下：

```text
U-mode 指令/中断
  -> 硬件保存 sepc/scause/stval/SPP/SPIE，清 SIE，PC <- stvec
  -> uservec                  （S-mode，用户页表，用户 sp）
  -> 保存用户通用寄存器      （写固定地址 TRAPFRAME）
  -> sp <- kernel_sp
  -> tp <- kernel_hartid
  -> 读取 kernel_trap/kernel_satp
  -> sfence.vma; satp <- kernel_satp; sfence.vma
  -> jalr usertrap            （S-mode，内核页表，内核栈）
```

逐步约束如下：

1. `uservec` 首先把用户 `a0` 放入 `sscratch`，再以 `a0 = TRAPFRAME` 保存除 `x0` 外的全部用户通用寄存器。
2. 从 trapframe 偏移 8 取出内核栈顶写入 `sp`。此时用户页表仍生效，但仅修改寄存器不会访问内核栈。
3. 从偏移 32 恢复内核 `tp`。内核把 `tp` 解释为 hart id，`mycpu()`/`cpuid()` 依赖这一约定。
4. 分别把 `usertrap()` 地址和内核 `satp` 读入 `t0`、`t1`。这些是切页表后无法再通过 `TRAPFRAME` 固定地址读取的数据。
5. 写 `satp` 前的第一条 `sfence.vma zero, zero` 在旧地址空间一侧建立地址翻译同步边界；写 `satp` 后的第二条清除本 hart 上可能陈旧的缓存翻译。`sfence.vma` 针对页表遍历和地址翻译，不是可替代普通内存屏障的通用栅栏；`zero, zero` 也只刷新当前 hart，不会替其他 hart 做 TLB shootdown。
6. trampoline 在内核页表的同一虚拟地址继续可执行。`jalr t0` 进入 C，函数序言此时才使用已经映射的内核栈。

这条入口没有安装 kernel `gp`。保存用户 `gp` 只是把它写入 trapframe，硬件寄存器 `gp` 本身仍保留用户值；`entry.S` 也没有建立一个全局 kernel `gp`。因此第 6 步能安全进入 C 还依赖当前编译产物不生成 `gp` 相对访问。这个工具链前提、当前反汇编证据和移植检查见[平台契约](../architecture/platform-contracts.md#5-psabi系统调用-abi-与汇编边界)。

`usertrap()` 的第一项检查要求 `sstatus.SPP == 0`，否则说明入口来源并非 U-mode，内核 panic。随后立刻把 `stvec` 改成 `kernelvec`。原因是 `usertrap()` 已运行在内核页表和内核栈；如果之后重新启用中断，却仍跳到假定“用户页表 + 用户现场”的 `uservec`，现场会被错误解释。

函数再取得当前进程，并立即把硬件 `sepc` 保存到 `p->trapframe->epc`。之后才按 `scause` 分派。

## 6. `usertrap()` 的分支语义

### 6.1 系统调用

用户桩由 `user/usys.pl` 生成，约定将系统调用号放进 `a7`，执行 `ecall`，然后 `ret`。`ecall` 产生 cause 8。

`usertrap()` 的处理顺序具有语义意义：

1. 若进程进入系统调用前已经被标记为 killed，立即 `kexit(-1)`，不执行系统调用。
2. `epc += 4`，跳过固定为 4 字节的 `ecall`。如果不前移，返回后会重复执行同一系统调用。
3. 此时 `sepc`、`scause`、`sstatus` 已经读取完，且 `stvec` 已指向 `kernelvec`，因此调用 `intr_on()` 允许系统调用执行期间被设备中断或时钟抢占。
4. `syscall()` 从 trapframe 的 `a7` 取编号，从 `a0` 到 `a5` 取参数，并把处理函数返回值写回 trapframe 的 `a0`。

未知或空缺的系统调用号由 `syscall()` 打印诊断并令 `a0 = -1`，不会杀死进程。系统调用可能睡眠、被时钟中断、让出 CPU，甚至在另一个 hart 上恢复；返回 `usertrap()` 后仍要重新检查 killed 状态。

### 6.2 外部设备和时钟中断

`devintr()` 先读取 `scause`：

- supervisor external interrupt：向 PLIC claim IRQ，调用 UART 或 VirtIO 处理器，complete 后返回 `1`。
- supervisor timer interrupt：调用 `clockintr()` 后返回 `2`。
- 其他原因：返回 `0`，让调用者继续尝试缺页分支或按未知 trap 处理。

用户态时钟中断返回 `2` 后，`usertrap()` 调用 `yield()`，把当前进程从 `RUNNING` 改为 `RUNNABLE` 并进入调度器。设备类型 `1` 不主动让出 CPU。共同的 killed 检查发生在 `yield()` 之前，所以在该检查时已经被杀死的进程会直接退出，不会再为这次时钟中断进入调度器；但这不是“从此绝不会再执行用户指令”的原子保证，后文会说明检查后的竞态窗口。

### 6.3 延迟分配缺页

只有 cause 13（读缺页）和 15（写/AMO 缺页）进入该分支：

```c
vmfault(p->pagetable, r_stval(), (r_scause() == 13) ? 1 : 0)
```

第三个参数在读缺页时为 `1`，写缺页时为 `0`。但是当前 `kernel/vm.c` 的 `vmfault(..., int read)` **没有使用 `read` 参数**；两种缺页现在得到相同的零填充、`PTE_R | PTE_W | PTE_U` 页面，而且都没有 `PTE_X`。不能据此声称当前实现按读写原因生成了不同权限。

`vmfault()` 还不是一个完全按形参工作的通用页表函数：它用传入的 `pagetable` 做 `ismapped()` 检查，最终 `mappages()` 却写 `myproc()->pagetable`。在这里 `usertrap()` 传入的正是当前进程页表，所以两者相同；其他调用者若传入临时页表，不能假定会把新页映射到该形参所指页表。

`vmfault()` 的成功条件是：

- fault VA 小于当前进程 `p->sz`；这是唯一的地址范围检查，代码没有另存“lazy heap 起点”，所以 `p->sz` 内任何尚未映射的页洞都可能按 lazy page 处理；
- 向下按页对齐后尚未映射；
- `kalloc()` 成功；
- `mappages()` 成功。

成功时页面清零并建立用户可读写映射，函数返回物理地址。`usertrap()` 不增加 `epc`，所以 `sret` 后原指令重新执行。`vmfault()` 和缺页分支本身没有立即执行 `sfence.vma`，但成功的 `usertrap()` 返回必经 `userret`：它在切换到用户 `satp` 前后各执行一次 `sfence.vma zero, zero`，因此本 hart 会在重试故障指令前清除旧的缓存翻译并看到新 PTE。若未来在不经过 `userret` 的路径复用 `vmfault()`，调用者仍需单独分析地址翻译同步。

以下情况返回 0，并最终把进程标记为 killed：`VA >= p->sz`、页面已经映射但发生权限错误、物理内存耗尽、页表建立失败。映射失败时刚分配的数据页会被 `kfree()`；`walk()` 在失败前已经建立的空中间页表页则可能保留到整个页表释放时。已映射检查也意味着写只读代码页、访问已映射但无用户权限的 guard page，不会被误当成 lazy allocation。instruction page fault、misaligned access 和非法指令同样不在此恢复范围。

### 6.4 未知用户 trap 与退出

无法识别或无法修复时，内核打印 `scause`、进程 pid、`sepc` 和 `stval`，再通过 `setkilled(p)` 在 `p->lock` 保护下置位。统一的分支后检查随即执行 `kexit(-1)`；它不会返回。

因此用户错误通常只终止当前进程，而不是拖垮内核。输出中出现 `usertrap(): unexpected scause ...` 不必自动等同于内核测试失败：`usertests` 中有用非法地址主动验证进程隔离的测试。

`kkill()` 本身只在 `p->lock` 下设置 `p->killed`，若目标在睡眠则把它改成 `RUNNABLE`；它不会发送 IPI，也不会直接销毁正在另一 hart 上运行的进程。系统调用分支在执行 `syscall()` 前检查一次，所有分支在分派后、可选的 `yield()` 前再检查一次。最后一次检查与 `prepare_return()`/`sret` 之间没有锁构成原子边界：若另一个 hart 恰在检查之后置位，或者目标在 `yield()` 中变为 `RUNNABLE` 后才被置位，当前源码没有第三次检查，进程可能先返回 U-mode，直到下一次系统调用、时钟中断或其他 trap 再由 `usertrap()` 观察并退出。因此 killed 是协作式的退出请求，不是“置位后零条用户指令”的同步终止保证。

## 7. 返回用户态：`prepare_return()` 与 `userret`

`prepare_return()` 有两个调用者：常规 trap 处理结束后的 `usertrap()`，以及新进程第一次被调度时的 `forkret()`。后者没有“先前的用户 trap”，但需要建立完全相同的返回条件。

返回序列为：

```text
prepare_return()             （内核页表、内核栈）
  -> intr_off()
  -> stvec <- trampoline 中的 uservec 虚拟地址
  -> 填写 kernel_satp/kernel_sp/kernel_trap/kernel_hartid
  -> sstatus.SPP <- 0, SPIE <- 1
  -> sepc <- trapframe.epc
  -> usertrap 返回用户 satp，或 forkret 以 a0 传入用户 satp
userret                      （起初仍是内核页表）
  -> sfence.vma; satp <- 用户 satp; sfence.vma
  -> 从 TRAPFRAME 恢复用户寄存器
  -> 最后恢复 a0
  -> sret                    （U-mode、用户页表、用户 sp）
```

`prepare_return()` 必须先关闭中断，再把 `stvec` 改成 `uservec`。从这次改写到最终 `sret` 之间，CPU 仍执行内核代码；如果异步中断在这段窗口到来，`uservec` 会错误地把当前内核寄存器当成用户现场。`intr_off()` 清除 `SIE`，封住同 hart 的异步 S-mode 中断窗口，但它不能屏蔽同步异常。因此返回走廊还依赖更强的不变量：trampoline 的代码映射、用户 `satp` 和 `TRAPFRAME` 映射必须有效，且汇编本身不能在 `sret` 前产生访问异常或非法指令。

它为下一次入口填写：

- `kernel_satp = r_satp()`：当前内核页表值；
- `kernel_sp = p->kstack + PGSIZE`：该进程内核栈顶；
- `kernel_trap = (uint64)usertrap`：C 入口地址；
- `kernel_hartid = r_tp()`：当前 hart id，允许进程迁移后使用新的 hart。

然后清 `SSTATUS_SPP`，使 `sret` 返回 U-mode；置 `SSTATUS_SPIE`，使 `sret` 执行 `SIE <- SPIE` 后令 `SIE = 1`；最后将保存的用户 `epc` 写入 `sepc`。`intr_off()` 已保证此刻 `SIE = 0`。这里把 `SIE` 准备为 1 是 `sret` 的状态恢复动作；如第 2 节所述，CPU 实际运行在 U-mode 时，S-mode 中断的全局递送并不以 `SIE` 为门槛。

`usertrap()` 以函数返回值形式把 `MAKE_SATP(p->pagetable)` 放入 `a0`。由于 `uservec` 的 `jalr` 返回地址正好落在紧随其后的 `userret`，C 返回便直接继续执行返回汇编。`forkret()` 则显式调用 trampoline 中 `userret` 的固定高地址，并将同一个 `satp` 值作为第一个参数传入。

`userret` 切到用户页表后只能依赖同时存在的 trampoline 和 `TRAPFRAME` 映射。它先恢复除 `x0`、`a0` 外的用户通用寄存器，再以仍指向 `TRAPFRAME` 的 `a0` 取回用户 `a0`；恒为零的 `x0` 无需恢复。最后 `sret` 原子地令 PC 取 `sepc`、当前特权级取 `SPP`（这里为 U-mode）、`SIE <- SPIE`，并按规范把 `SPIE` 置 1、`SPP` 置 0。`stvec` 保持指向 `uservec`，为下一次用户 trap 做准备。

## 8. 内核态 trap：`kernelvec` 与 `kerneltrap()`

`trapinithart()` 在每个 hart 上执行 `w_stvec((uint64)kernelvec)`。`stvec` 是每 hart CSR，不是设置一次即可供所有 CPU 使用。进入或返回用户态时它会临时改成该 hart 上的 `uservec` 地址；每次用户 trap 的 C 入口又改回 `kernelvec`。

从 S-mode 发生 trap 时，CPU 已位于内核页表和当前内核执行流所用的内核栈。`kernelvec` 在当前 `sp` 下预留 256 字节，保持 16 字节栈对齐，保存 `ra`、`gp`，以及 ABI 中 caller-saved 的 `t0-t6` 和 `a0-a7`，再调用 `kerneltrap()`。这里保存/恢复 `gp` 只保持被打断流原有的值，不会初始化它；C 代码仍受上一节所述的“不得生成 `gp` 相对访问”约束。256 字节布局保留了与其他寄存器对应的空槽；代码不会初始化或读取这些空槽。

它没有逐一保存 `s0-s11`，因为按 RISC-V C ABI，作为被调用函数的 `kerneltrap()` 及其下游必须保存这些 callee-saved 寄存器；也没有保存 `sp`，因为栈帧由固定的减/加 256 恢复。`tp` 不恢复：xv6 在内核中用它保存当前 hart id，而 trap 中的 `yield()` 可能让进程之后在另一个 CPU 恢复，必须保留恢复执行时的 hart id。

`kerneltrap()` 先把 `sepc`、`sstatus`、`scause` 读入 C 局部值，再断言：

- `sstatus.SPP != 0`，来源必须是 S-mode；
- `intr_get() == 0`，trap 入口时 SIE 必须已经由硬件关闭。

内核 trap 只接受 `devintr()` 识别的中断。未知异常或中断打印 CSR 后 panic；与用户错误不同，内核访问非法地址表明内核不变量已破坏，没有可安全隔离的进程边界。

时钟中断且 `myproc() != 0` 时调用 `yield()`。若 CPU 正在调度器上下文运行，`myproc()` 为 0，只重置时钟而不调度。`yield()` 期间可能切走、迁移并经历更多 trap，所以返回后必须显式恢复入口读到的 `sepc` 和 `sstatus`，再由 `kernelvec` 恢复通用寄存器并执行 `sret`。这里保存的是硬件完成 trap 入口更新后的 `sstatus`：其 `SIE` 已为 0，`SPIE` 记录被打断内核代码原来的 `SIE`。恢复该值后，最终 `sret` 才能把被打断时的可中断状态还原。`scause` 只用于分派/诊断，`sret` 不消费它，因此无需恢复。若直接使用调度期间遗留的 `sepc`/`sstatus`，内核会返回错误 PC 或错误中断状态。

## 9. 时钟中断、PLIC 与设备确认

### 9.1 时钟路径

每个 hart 在 `start()` 中同时设置 `menvcfg.STCE` 与 `mcounteren.TM`；二者共同使已由硬件实现的 Sstc `stimecmp` 可供 S-mode 使用，TM 还开放 `time`，然后首次设置：

```text
stimecmp = time + 1,000,000
```

时钟到期后，`clockintr()` 在每个 hart 上重新写入相同间隔；该写入同时清除当前请求并安排下一次中断。按源码注释，这大约是 0.1 秒，实际墙钟时间依赖 QEMU 的 timebase。

所有 hart 的 timer 都可触发抢占，但只有 `cpuid() == 0` 执行全局时间推进：

```text
acquire(tickslock)
  ticks++
  wakeup(&ticks)
release(tickslock)
```

`sys_pause()` 等待的就是 `ticks` 变化。只让 hart 0 更新它可避免多核数量改变系统“滴答”速度。`tickslock` 保护计数和 sleep/wakeup 条件；`sleep(&ticks, &tickslock)` 通过先取得进程锁再释放条件锁，避免错过唤醒。

### 9.2 PLIC 路径

启动 hart 0 的 `plicinit()` 把 UART IRQ 10 和 VirtIO IRQ 1 的优先级设为 1；优先级 0 等价于禁用。每个 hart 的 `plicinithart()` 再：

1. 在本 hart 的 S-mode enable bitmap 中打开 IRQ 10 和 IRQ 1；
2. 把优先级阈值设为 0，使优先级 1 可以递送。

收到 supervisor external interrupt 后，协议是：

```text
irq = plic_claim()
  irq == UART0_IRQ   -> uartintr()
  irq == VIRTIO0_IRQ -> virtio_disk_intr()
  其他非零 irq       -> 打印 unexpected interrupt
plic_complete(irq)   -> 仅 irq 非零时执行
```

claim 既取得当前最高优先级 IRQ，也把它标记为正在处理；在 complete 前，同一源不能再次发起可递送中断。因此任何已 claim 的非零 IRQ 都必须 complete，包括当前代码不认识而只打印的 IRQ。若 claim 返回 0，代码仍把这次 external interrupt 视为已处理并返回设备类型 `1`，但不会调用 complete。

PLIC 的 complete 只完成中断控制器这一层的握手，不能替代设备自身的确认。UART 路径先读取 ISR，并消费接收数据或清除 `tx_busy`；VirtIO 路径把 MMIO interrupt status 的低两位写入 ACK，再消费 used ring 并唤醒请求线程。设备处理器返回后，`devintr()` 才执行 PLIC complete。漏掉设备级确认可能反复触发同一条件，漏掉 PLIC complete 则会阻止网关继续转交该源的后续请求。

PLIC 的 enable、threshold、claim/complete 寄存器都是按 hart 寻址的，`cpuid()` 必须反映当前 CPU。这也是 `uservec` 恢复 `kernel_hartid`、`kernelvec` 不恢复旧 `tp` 的直接原因。

## 10. 锁、中断与并发不变量

修改 trap 代码时必须同时维持以下约束：

1. **硬件入口不等于完整上下文切换。** U-mode 入口在保存最后一个用户寄存器前，不能随意占用通用寄存器；使用寄存器前必须已有明确的保存位置。
2. **切换 `satp` 时 PC 必须仍可取指。** trampoline 必须在用户和内核页表的相同 VA 映射同一物理页，并在页表切换前后执行所需的本 hart 地址翻译同步。
3. **内核栈只能在内核页表生效后访问。** `uservec` 可以先更新 `sp`，但在安装内核 `satp` 前不能压栈或调用 C。
4. **把 `stvec` 指向 `uservec` 的窗口内必须关闭中断，且不得发生同步 fault。** `prepare_return()` 的 `intr_off()` 不能后移到 `w_stvec()` 之后；它不替代对 trampoline、trapframe 和 `satp` 有效性的保证。
5. **重新打开中断前先保存仍需使用的 CSR 状态。** 系统调用分支只有在把 `sepc` 保存到 `epc`、消费完 `scause`/`sstatus` 所需信息、前移 PC 且切换 `stvec` 后才执行 `intr_on()`；代码没有把整个 CSR 集合复制到 trapframe。
6. **持有自旋锁时中断关闭。** `acquire()` 的 `push_off()` 防止当前 CPU 在持锁时进入会争用同一锁的中断处理；`release()` 的 `pop_off()` 只在嵌套计数回到零且原先允许中断时恢复 SIE。
7. **timer 抢占不能发生在持自旋锁区间。** 锁关闭本地中断保证 `kerneltrap()` 的 timer 分支调用 `yield()` 时不携带任意自旋锁；`sched()` 只允许持有 `p->lock`，且要求中断关闭。
8. **killed 状态由 `p->lock` 同步，但检查与返回不是同一临界区。** `setkilled()`/`killed()` 负责锁操作；trap 代码只在可安全退出的边界调用 `kexit(-1)`。检查后到 `sret` 的竞态只会推迟到下一次 trap 退出，不能把 killed 理解为同步撤销用户执行权。
9. **`ticks` 和等待条件由 `tickslock` 保护。** timer 中断持有该锁时调用 `wakeup(&ticks)`，等待者用同一锁进入 `sleep()`，形成不丢事件的条件同步。
10. **`sepc`/`sstatus` 是每 hart 的易变状态。** 内核 trap 若可能调度，必须在调度前保存、返回后恢复；不能假定嵌套 trap 或迁移不会改写它们。

用户系统调用期间显式开启中断，因此允许嵌套的“内核态中断 trap”：外层是 `usertrap()`，内层的设备或时钟中断经 `kernelvec`/`kerneltrap()`。其他用户异常分支没有显式开启中断，保持入口时的关闭状态直到 `sret` 按 `SPIE` 恢复。

## 11. 失败路径与诊断含义

| 失败点 | 行为 | 为什么这样处理 |
| --- | --- | --- |
| `usertrap()` 发现 `SPP != 0` | panic | `uservec`/trapframe 前提已不可信 |
| 用户未知 trap | 打印 CSR，置 killed，`kexit(-1)` | 隔离到故障进程 |
| lazy fault 地址/权限非法或 OOM | 置 killed，`kexit(-1)` | 无映射可供原指令重试 |
| killed 在最后一次检查后到达 | 本次可能仍 `sret`；下一次 trap 再退出 | killed 检查与返回不是原子事务，`kkill()` 也不发送 IPI |
| 系统调用号非法 | 打印诊断，`a0 = -1` | ABI 级可恢复输入错误 |
| 未知非零 PLIC IRQ | 打印 IRQ，仍 complete | 防止中断源永久卡在 in-service 状态 |
| PLIC claim 返回 0 | 不 complete，返回已处理 | 视为外部中断的空 claim/竞态情形 |
| `kerneltrap()` 来源不是 S-mode | panic | 内核 trap 入口契约被破坏 |
| `kerneltrap()` 发现 SIE 开启 | panic | 与硬件 trap 入口语义矛盾 |
| 内核未知 trap | 打印 CSR 并 panic | 内核自身故障不能可靠地进程隔离 |

诊断三个 CSR 时：`scause` 回答“为何进入”，`sepc` 回答“哪条指令附近进入”，`stval` 回答“相关地址或附加值是什么”。对于缺页，先用 `stval` 定位 VA，再检查 `p->sz`、对应 PTE 的 `V/U/R/W/X` 位和 fault 类型；只看 `sepc` 往往不足以解释权限错误。

## 12. 修改影响清单

以下改动必须跨文件同步审查：

- 调整 `struct trapframe`：同步 `trampoline.S` 的全部偏移，并验证结构仍装得下一页。
- 改 `TRAMPOLINE`/`TRAPFRAME`：同步用户页表、内核页表、`stvec` 和 `forkret()` 的地址计算。
- 改 trampoline 段：保持同 VA 双映射、页对齐、可执行权限和 `kernel.ld` 一页断言。
- 增加用户异常：确定是可恢复、杀进程还是 panic；可恢复异常通常不能像 `ecall` 一样无条件 `epc += 4`。
- 增加 PLIC 设备：同时设置优先级、每 hart enable、IRQ 分派和 complete。
- 改 timer 频率：同时评估 `ticks`、`pause()` 语义、抢占频率及多核开销。
- 在 trap 路径加锁或睡眠：核对 SIE、`push_off()` 嵌套、锁顺序，以及 `sched()` 的“只持有 `p->lock`”要求。
- 为 lazy fault 增加读写权限差异：不能只使用现有 `read` 参数名，必须明确 cause 13/15 的权限策略、已映射权限 fault 和测试预期。

## 13. 测试与 GDB 验证

### 13.1 回归测试

先验证构建，再分别覆盖单核和多核，因为 PLIC、timer、`tp` 与迁移问题常只在多核出现：

```sh
make
make qemu CPUS=1
make qemu                 # Makefile 默认 CPUS=3
```

在 xv6 shell 中至少运行：

```text
usertests -q
grind
```

重点观察 `user/usertests.c` 中以下用例：

- `lazy_alloc`、`lazy_unmap`、`lazy_sbrk`：覆盖用户缺页、回收与高地址边界；`lazy_copy` 只对 lazy 字符串调用一次未检查返回值的 `open()`，并强断言坏高地址 I/O 失败和异常负缩容不破坏 break，不能单独证明合法 lazy buffer 的 `copyin/copyout` 补页；
- `sbrkbasic`、`sbrkfail`、`sbrkarg`、`sbrkbugs`、`sbrklast`、`sbrk8000`：覆盖 OOM、越界、缩减和非法访问后的 killed 路径；
- `preempt`：覆盖时钟中断使忙循环让出 CPU；
- `killstatus`：覆盖 killed 状态最终以 `-1` 退出；
- 普通文件、pipe 和 console 测试：间接覆盖 VirtIO/UART 中断、睡眠和唤醒。

故意触发非法用户访问的测试可能打印 `usertrap()` 诊断；判断结果应看测试最终状态以及内核是否继续服务其他进程，不能只按日志中是否出现 `unexpected scause` 判断。

### 13.2 GDB 观察入口与 CSR

在一个终端启动暂停的 QEMU：

```sh
make qemu-gdb CPUS=1
```

在另一个终端连接仓库配置的 RISC-V GDB。可设置这些断点：

```gdb
break uservec
break usertrap
break syscall
break devintr
break clockintr
break kerneltrap
break prepare_return
break userret
continue
```

符号 `uservec`/`userret` 的链接地址与实际用户页表入口 VA 可能不同。若普通符号断点没有命中，可先在 GDB 求偏移，再对 trampoline 高地址设硬件断点：

```gdb
p/x (unsigned long)uservec - (unsigned long)trampoline
p/x (unsigned long)userret - (unsigned long)trampoline
p/x (unsigned long)TRAMPOLINE
```

随后使用 `TRAMPOLINE + offset` 的结果设置 `hbreak *地址`。如果宏不可见，也可直接打印 `prepare_return()` 中算出的 `trampoline_uservec`，或查看 `stvec` 的运行时值。

命中 `usertrap()` 或 `kerneltrap()` 后，检查 CSR：

```gdb
info registers sepc scause stval sstatus satp stvec sp tp
p/x *myproc()->trapframe
p/x myproc()->kstack + 4096
```

建议按以下断言逐项验证：

1. `uservec` 刚进入时是 S-mode，但 `satp` 仍为用户页表，`sp` 仍是用户栈。
2. `usertrap()` 中 `sp` 位于 `[p->kstack, p->kstack + PGSIZE)`，`tp` 等于当前 hart id，`satp` 是内核页表。
3. 在系统调用分支，保存到 `trapframe.epc` 的值比最终返回 PC 少 4；trapframe 的 `a7` 是系统调用号，`a0` 最后变成返回值。
4. lazy fault 时 `scause` 为 13 或 15，`stval < p->sz`；成功后相应 PTE 变成有效、用户可读写，`epc` 不变。
5. `prepare_return()` 后 `stvec` 指向 trampoline 中的 `uservec`，`SPP=0`、`SPIE=1`、`SIE=0`，`sepc == p->trapframe->epc`。
6. `kerneltrap()` 中入口 `SIE=0`；经过 timer `yield()` 后，`sepc` 和 `sstatus` 恢复为进入前保存值。

调试多核中断时再以默认 `CPUS=3` 启动，并查看 `tp`、PLIC 的 hart 索引和 `ticks`：timer 可在任意 hart 命中，但只有 hart 0 增加 `ticks`。单步跨越 `satp` 写入时，GDB 对虚拟地址和断点的显示可能受 QEMU stub 地址翻译影响；应同时用 `satp`、当前 PC 和固定 trampoline VA 判断所在阶段。

## 14. 一次用户系统调用往返的最终检查

可以用下面这条压缩链条检查对整体实现的理解：

```text
用户桩把编号放入 a7
  -> ecall
  -> 硬件跳 uservec，尚未换页表/栈
  -> trapframe 保存除 x0 外的全部用户通用寄存器
  -> 切内核栈、tp、satp
  -> usertrap 保存 sepc，令 epc += 4，切 stvec，开中断
  -> syscall 从 trapframe 取参数并写回 a0
  -> prepare_return 关中断，准备 stvec/sstatus/sepc 和下次入口字段
  -> usertrap 返回用户 satp 到 a0，控制流落入 userret
  -> 切用户页表，恢复寄存器
  -> sret 恢复 U-mode、用户 PC 和可中断状态
```

这条链中任何一步的地址空间、栈、`stvec` 或 SIE 前提出错，都不会只影响一个函数，而会破坏整个用户态/内核态边界。因此 trap 子系统的正确性来自这些跨 C、汇编、页表、调度器和中断控制器的共同不变量，而不是某个单独处理函数。
