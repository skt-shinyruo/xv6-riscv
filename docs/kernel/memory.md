# 物理内存与虚拟内存

本文以当前仓库实现为准，说明物理页分配器、RISC-V Sv39 页表、内核和用户地址空间、用户指针拷贝，以及本仓库扩展的 lazy allocation。这里的重点不是逐行复述源码，而是说明页的所有权、映射的生命周期、跨页访问的实际行为、并发前提和失败后的资源状态。

本文不展开 trap 汇编的完整寄存器保存协议、ELF 格式或系统调用分派；这些内容分别属于 trap、`exec` 和系统调用文档。不过，只要它们影响页表切换或内存所有权，本文会给出必要的调用关系。

## 1. 源码地图

核心文件如下：

| 文件 | 责任 | 核心符号 |
|---|---|---|
| `kernel/kalloc.c` | 4 KiB 物理页的初始化、分配和回收 | `kinit()`、`freerange()`、`kalloc()`、`kfree()` |
| `kernel/vm.c` | 页表创建、遍历、映射、用户地址空间生命周期、用户拷贝和 lazy fault | `kvmmake()`、`walk()`、`mappages()`、`uvmcopy()`、`copyout()`、`vmfault()` |
| `kernel/vm.h` | `sbrk` 的 eager/lazy 模式编号 | `SBRK_EAGER`、`SBRK_LAZY` |
| `kernel/memlayout.h` | QEMU `virt` 物理布局及内核/用户保留虚拟地址 | `KERNBASE`、`PHYSTOP`、`KSTACK()`、`TRAMPOLINE`、`TRAPFRAME` |
| `kernel/riscv.h` | 页大小、PTE 编码、Sv39 索引、`satp` 和 TLB 操作 | `MAKE_SATP`、`sfence_vma()`、`MAXVA` |

当前源码树没有独立的 `kernel/vmfault.c`；`vmfault()` 和 `ismapped()` 都定义在 `kernel/vm.c`。若其他分支把 fault 处理拆到单独文件，不能据此推断本仓库也有相同的编译单元或接口边界。

理解完整生命周期还需要结合：

| 文件 | 与本文的关系 |
|---|---|
| `kernel/main.c` | CPU 0 按 `kinit -> kvminit -> kvminithart` 顺序启用分页 |
| `kernel/proc.c` | 分配内核栈/trapframe、创建进程页表、`fork`、增长/缩减地址空间和最终释放 |
| `kernel/exec.c` | 创建并提交全新的用户页表，建立 ELF 段、保护页和用户栈 |
| `kernel/trap.c` | 把用户 load/store page fault 交给 `vmfault()`，准备用户 `satp` |
| `kernel/trampoline.S` | 在用户页表和全局内核页表之间切换，并在两次切换周围刷新 TLB |
| `kernel/kernel.ld` | 定义 `etext` 和 `end`，分别划分代码权限与物理页池起点 |
| `kernel/sysproc.c` | `sys_sbrk()` 选择 eager 或 lazy 增长策略 |
| `user/ulib.c` | `sbrk()` 和本仓库特有的 `sbrklazy()` 包装器 |
| `user/usertests.c` | 地址边界、回滚、页泄漏、lazy fault 和用户拷贝测试 |

## 2. 两层地址模型

xv6 先管理物理页，再用页表给这些页建立虚拟地址。两层必须分开理解：

```text
物理页所有权                     虚拟映射

kmem.freelist --kalloc()--> owner --mappages()--> VA -> PTE -> PA
owner          --kfree()---> kmem.freelist

页被映射不等于分配器知道其引用数；
清除 PTE 也不一定释放物理页，取决于 uvmunmap(..., do_free)。
```

当前分配器没有引用计数。一个 `kalloc()` 返回的页在任一时刻必须只有一个明确的释放责任方；共享映射必须指定唯一 owner，并在解除非 owning 映射时传 `do_free == 0`。

### 2.1 QEMU `virt` 的物理地址

`kernel/memlayout.h` 记录的关键物理地址是：

| 范围/地址 | 用途 |
|---|---|
| `0x00001000` | QEMU boot ROM |
| `0x02000000` | CLINT 所在区域；当前定时器路径使用 Sstc，不由 `kvmmake()` 映射 CLINT |
| `0x0c000000` | PLIC 起点 |
| `0x10000000` | UART0 MMIO，IRQ 10 |
| `0x10001000` | VirtIO block MMIO，IRQ 1 |
| `0x80000000` (`KERNBASE`) | QEMU 装载并进入内核的位置 |
| `end` | 链接后内核 text/rodata/data/bss 的末尾 |
| `0x88000000` (`PHYSTOP`) | xv6 使用的 RAM 上界，等于 `KERNBASE + 128 MiB` |

QEMU 的 Makefile 同样以 `-m 128M` 启动，因此 `PHYSTOP` 与当前虚拟机 RAM 配置一致。`[PGROUNDUP(end), PHYSTOP)` 才进入通用物理页池；内核镜像、MMIO 区域以及 `PHYSTOP` 以上地址都不能交给 `kfree()`。

### 2.2 用户虚拟地址

`riscv.h` 把 `MAXVA` 定义为 `1 << 38`，即 `0x4000000000`。它不是一个可用地址，而是所有被 `walk()` 接受的地址的上界。最高两个保留页为：

```text
0x4000000000  MAXVA，禁止访问
0x3ffffff000  TRAMPOLINE：同一份 trampoline 代码，用户不可访问
0x3fffffe000  TRAPFRAME：当前进程 trapframe，用户不可访问
                  ...
                  未使用的地址空间
                  ...
p->sz          用户已声明地址空间的逻辑末尾
                  heap（由 sbrk/sbrklazy 增长）
                  用户栈（当前 USERSTACK == 1 页）
                  stack guard（已映射但清除 PTE_U）
                  ELF data/bss/text
0x0
```

用户栈不是固定放在 `TRAPFRAME` 下方。`exec` 把它放在 ELF 映像之后，再由 heap 从 `p->sz` 向高地址增长。`TRAPFRAME` 是普通用户内存增长不能越过的硬上界。

`TRAMPOLINE` 和 `TRAPFRAME` 在每个进程页表中都没有 `PTE_U`。这两个映射供 supervisor 模式下的 trampoline 使用；用户模式不能直接读取或写入它们。

## 3. 物理页分配器

### 3.1 数据结构与页内元数据

`kernel/kalloc.c` 使用单向空闲链表：

```c
struct run {
  struct run *next;
};

struct {
  struct spinlock lock;
  struct run *freelist;
} kmem;
```

空闲页本身保存 `struct run`，所以不需要额外的元数据数组。页一旦分配给调用者，页首的 `next` 就不再有意义，整页 4096 字节都归调用者使用。

`kmem.lock` 只保护 `freelist` 链接关系，不保护已经分配出去的页，也不追踪这些页被映射到哪里。

### 3.2 初始化

CPU 0 在 `main()` 中调用 `kinit()`：

```text
kernel.ld 产生 end
  -> kinit 初始化 kmem.lock
  -> freerange(end, PHYSTOP)
  -> 从 PGROUNDUP(end) 起逐页调用 kfree
  -> 所有可用页进入 freelist
```

`freerange()` 的循环条件是 `p + PGSIZE <= pa_end`，因此只释放完整页。初始化是 `kfree()` 的特例：这些页此前没有由 `kalloc()` 返回，但地址范围和对齐仍满足 `kfree()` 的契约。

### 3.3 `kalloc()`

`kalloc()` 在持有 `kmem.lock` 时取出链表头，然后立即释放锁。成功时，它在页中填入字节 `5`，再把物理地址作为内核可解引用的指针返回；链表为空时返回 `0`。

填充 `5` 不是初始化语义。需要零页的调用者必须显式 `memset(page, 0, PGSIZE)`；`uvmalloc()`、`uvmcreate()`、`walk()` 创建页表页以及 `vmfault()` 都这样做。将新页先填为非零值有助于暴露“误以为分配结果自动清零”的错误。

返回地址可以直接作为内核指针，是因为可分配 RAM 位于内核页表的恒等映射区域，虚拟地址与物理地址数值相同。分页启用之前，同一个数值本来就是物理地址。

### 3.4 `kfree()`

`kfree(pa)` 首先验证三个不变量：

- `pa` 必须按 `PGSIZE` 对齐；
- `pa >= end`，不能释放内核镜像；
- `pa < PHYSTOP`，不能释放 RAM 上界以外的地址。

违反任一条件会 `panic("kfree")`。通过检查后，整页先填为字节 `1`，再把它作为 `struct run` 插到链表头。填充 `1` 可让一部分 use-after-free 更快表现为损坏，但它不是完整的悬空引用检测器。

`kfree()` 不检测双重释放。同一页第二次入链会破坏 freelist，后果可能延迟到之后的分配才出现。因此“每个分配页只有一个释放者”是调用者必须维持的核心不变量。

### 3.5 并发和发布顺序

物理页分配器的并发顺序是：

```text
kfree: 独占 owner 写 poison -> acquire kmem.lock -> 发布到 freelist -> release
kalloc: acquire kmem.lock -> 从 freelist 移除 -> release -> 新 owner 写 poison
```

poison 写发生在页仍由当前执行流独占时，链表修改发生在锁内。`kmem.lock` 是 spinlock，获取它会按全局 spinlock 协议关闭本 hart 的中断；`kalloc()` 和 `kfree()` 都不会睡眠。

调用者不能持有 `kmem.lock` 再进入可能调用 `kalloc()`/`kfree()` 的页表函数，否则会递归获取同一 spinlock。当前代码没有这样的调用路径。

### 3.6 典型所有权

| 页的用途 | 创建者 | 最终释放者 |
|---|---|---|
| 内核页表根和中间页 | `kvmmake()`/`walk()` | 内核运行期间不释放 |
| 每个进程槽的内核栈 | `proc_mapstacks()` | 内核运行期间不释放 |
| 用户页表根和中间页 | `uvmcreate()`/`walk()` | `freewalk()` |
| 用户 text/data/heap/stack 页 | `uvmalloc()`、`vmfault()`、`uvmcopy()` | `uvmunmap(..., 1)`，通常经 `uvmfree()` |
| 用户栈 guard 页 | `uvmalloc()` | 虽然用户不可访问，仍由 `uvmfree()` 释放 |
| `p->trapframe` 页 | `allocproc()` | `freeproc()` 直接 `kfree()` |
| trampoline 代码页 | 内核镜像 | 不由页分配器释放；进程页表解除映射时 `do_free == 0` |
| pipe 对象页 | `pipealloc()` | `pipeclose()` 在两端都关闭后释放 |

## 4. Sv39 在当前实现中的表示

### 4.1 地址拆分

Sv39 使用三级页表。当前实现只建立 4 KiB 叶映射：

```text
虚拟地址（当前 xv6 只接受 0 <= va < MAXVA）

  38       30 29       21 20       12 11          0
 +-----------+-----------+-----------+--------------+
 | PX(2),9bit| PX(1),9bit| PX(0),9bit| offset,12bit |
 +-----------+-----------+-----------+--------------+
       |           |           |
       v           v           v
   level-2 root -> level-1 -> level-0 PTE -> 4 KiB physical page
```

每张页表恰好一页，包含 512 个 64 位 PTE。`pagetable_t` 是 `uint64 *`，`pte_t` 是 `uint64`。`PXSHIFT(level)` 和 `PX(level, va)` 分别计算索引位移与 9 位索引。

硬件 Sv39 允许高半 canonical 地址，但 xv6 把 `MAXVA` 设为完整 Sv39 范围的一半，使可用地址的符号位保持为 0，避免在 C 代码中处理高位符号扩展。结果是 level-2 的最高索引位始终为 0。

### 4.2 PTE 编码

当前代码使用以下标志：

| 标志 | 含义 |
|---|---|
| `PTE_V` | PTE 有效 |
| `PTE_R` | 可读 |
| `PTE_W` | 可写 |
| `PTE_X` | 可执行 |
| `PTE_U` | 用户模式可访问 |

非叶 PTE 只设置 `PTE_V`，其 PPN 指向下一层页表。叶 PTE 设置 `PTE_V` 且至少具有 `R/W/X` 中的权限位。`freewalk()` 正是用“有效且没有任何 `R/W/X`”区分中间页表，用“有效且存在 `R/W/X`”识别尚未解除的叶映射。

`PA2PTE(pa)` 把页对齐物理地址的页号移到 PTE 的 PPN 字段；`PTE2PA(pte)` 做逆变换；`PTE_FLAGS(pte)` 保留低 10 位。`uvmcopy()` 使用 `PTE_FLAGS()` 复制父进程页权限，因此硬件可能写入的低位状态也会随标志一起复制。

### 4.3 Accessed/Dirty 位的平台前提

Sv39 叶 PTE 的低位还包括 Accessed（A）和 Dirty（D）。当前 `kernel/riscv.h` 没有为它们定义 `PTE_A/PTE_D`；在标志位上，`mappages()` 只是写入调用者提供的 `perm` 再加 `PTE_V`，不会自行补 A/D。`uvmalloc()`、lazy fault 和内核初始映射等普通新建路径只传 `R/W/X/U` 的适当组合，所以这些新叶的 A/D 初值为 0；`uvmcopy()` 则会通过 `PTE_FLAGS()` 传入并继承父叶已有的 A/D。内核没有识别“有效叶 PTE 仅因 A 或 D 为零而 fault”的软件模拟路径：用户 load/store A/D fault 交给 `vmfault()` 后，`ismapped()` 会看到该页已有 `PTE_V` 并拒绝把它当作 lazy hole；instruction page fault 不调用 `vmfault()`。在 `stvec` 已指向 `kernelvec` 的普通内核 C 执行窗口中，内核态同类 fault 会进入 `kerneltrap()`，并因 `devintr()` 无法识别该异常而 panic。

这个 `kerneltrap()` 结论不能泛化到任意 supervisor 指令窗口：早期启动在 `trapinithart()` 前尚未安装 `kernelvec`；返回方向上，`prepare_return()` 会在仍处于 supervisor 模式时把 `stvec` 改指向 `uservec`，而 `sret` 不修改 `stvec`；进入方向上，从硬件进入 `uservec` 到 `usertrap()` 执行 `w_stvec(kernelvec)` 前，CPU 也已经处于 supervisor 模式但 `stvec` 仍指向 `uservec`。这些过渡窗口若意外 fault，并不受“必经 `kerneltrap()`”保证保护，可能进入不适用于当前上下文的入口。

因此当前实现依赖平台采用硬件更新方案：硬件可以推测性地更新 A，D 则必须由实际写访问触发，而不是要求 supervisor 通过页故障逐项置位。QEMU `virt` 的当前配置满足这个前提。移植到启用 Svade 或其他软件管理 A/D 的环境时，可以在发布叶 PTE 前预置适当位；若要保留按访问置位的语义，则必须新增专门 fault 处理，并在修改 PTE 后执行适当的 `sfence.vma`。这是整套页表创建和 trap 路径的平台适配，不只是 lazy allocation：内核直映、内核 text、trampoline 和普通用户映射也要一并处理。

`PTE_FLAGS()` 保留全部低 10 位，所以 `uvmcopy()` 会把父叶当时的 A/D 状态一起复制给 child：父 PTE 中已置的位在 child 中也已置，仍为零的位则保持为零并等待平台后续处理。由于 A 允许推测性更新，不能仅凭 A 推断父页是否真的被执行过显式访问。xv6 不读取这些位做页替换、写回或统计，因此这种继承不改变当前功能，只是精确的 PTE ABI 事实。

当前 `walk()` 总是下降到 level 0。它不识别 level 2/1 的大页叶 PTE；如果加入 superpage，必须同步修改 `walk()`、解除映射和释放逻辑，不能只手工放置上层叶条目。

### 4.4 `satp` 与 TLB

`MAKE_SATP(pagetable)` 由两部分组成：

```text
SATP_SV39（MODE=8） | 根页表物理地址 >> 12
```

它没有设置 ASID，因此所有地址空间使用 ASID 0。`w_satp()` 写 supervisor address translation and protection 寄存器；`sfence_vma()` 执行 `sfence.vma zero, zero`，刷新当前 hart 的全部 TLB 项。

当前实现使用全量刷新而不是按地址或 ASID 精细失效：

- `kvminithart()` 在安装 `kernel_pagetable` 前后各刷新一次；
- trampoline 从用户页表切到内核页表前后刷新；
- trampoline 从内核页表切回用户页表前后也刷新。

`sfence.vma` 只影响当前 hart。当前设计不需要跨 hart TLB shootdown，因为同一进程不会同时在多个 hart 执行，运行中的用户页表只由该进程自身在 trap 后修改；全局内核页表则在其他 hart 启用分页前构造完成，之后保持不变。若未来加入共享地址空间或运行期内核映射，这个前提将失效。

## 5. 页表基础原语

### 5.1 `walk()`：查找或创建页表路径

`walk(pagetable, va, alloc)` 返回 level-0 PTE 的地址，而不是物理页地址：

1. `va >= MAXVA` 立即 panic；这表示内核调用者违反了可信参数契约。
2. 从 level 2 迭代到 level 1。
3. 若当前 PTE 有 `PTE_V`，用 `PTE2PA()` 把它当作下一层页表。
4. 若条目无效且 `alloc == 0`，返回 `0`。
5. 若条目无效且 `alloc != 0`，用 `kalloc()` 创建并清零下一层页表，再发布仅含 `PTE_V` 的非叶 PTE。
6. 返回 level-0 的 `&pagetable[PX(0, va)]`；该叶条目本身可能仍无效。

这依赖一个强不变量：level 2/1 中的有效条目必须是非叶页表指针。`walk()` 不检查 `R/W/X`，所以把上层叶 PTE 交给它会把普通数据页误当作页表页。

`alloc != 0` 时，若第二次中间页分配失败，第一次已经安装的空页表不会在 `walk()` 内回滚。它仍属于该页表，最终由 `freewalk()` 释放。

### 5.2 `mappages()`：安装叶映射

`mappages(pagetable, va, size, pa, perm)` 为连续页安装 PTE。这个仓库的实现比一些 xv6 版本更严格：

- `va` 必须页对齐，否则 panic；
- `size` 必须是 `PGSIZE` 的整数倍，否则 panic；
- `size` 不能为 0，否则 panic；
- 目标叶 PTE 不能已经有效，否则以 `mappages: remap` panic。

每个叶条目写为 `PA2PTE(pa) | perm | PTE_V`。函数不验证物理范围，不自动增加引用计数，也不刷新 TLB。它相信调用者传入合法、拥有的物理页和适当权限。

`walk(..., 1)` 因内存耗尽失败时，`mappages()` 返回 `-1`。若一次调用映射多个页，它不会撤销此前已经安装的叶条目。当前用户内存路径通常每次只调用它映射一页；内核启动的大范围映射失败则由 `kvmmap()` 直接 panic。

### 5.3 `walkaddr()`：用户 VA 到物理页

`walkaddr(pagetable, va)` 返回包含 `va` 的物理页基址，失败返回 0。它检查：

- `va < MAXVA`；
- 页表路径存在；
- 叶 PTE 有 `PTE_V`；
- 叶 PTE 有 `PTE_U`。

它不把页内 offset 加到结果中，调用者必须自行加 `va - PGROUNDDOWN(va)`。它也不检查 `PTE_R`、`PTE_W` 或 `PTE_X`；`copyout()` 额外检查写权限，而 `copyin()`/`copyinstr()` 依赖当前所有用户可访问页都带 `PTE_R` 的构造规则。

`walkaddr()` 只适合查询用户页。没有 `PTE_U` 的 stack guard、`TRAPFRAME` 和 `TRAMPOLINE` 都会返回 0，即使其 PTE 有效。

### 5.4 `ismapped()`

`ismapped(pagetable, va)` 调用 `walk(..., 0)`，只要叶位置的 PTE 有 `PTE_V` 就返回 1。它不要求 `PTE_U`，也不检查读写权限。`vmfault()` 用它避免把 stack guard 或其他已经存在但权限不足的映射当作 lazy 空洞重新映射。

调用者必须保证 `va < MAXVA`；否则内部 `walk()` 会 panic。

### 5.5 对齐宏

`PGROUNDDOWN(a)` 清除低 12 位，取得页首；`PGROUNDUP(sz)` 把非对齐大小向上取整到下一页。两者被用于：

- 将 fault 地址归入一页；
- 从 `end` 找到第一个完整可分配页；
- 只在缩减跨过页边界时真正释放页；
- 把按字节记录的 `p->sz` 转换成页数。

这些宏不做整数溢出检测。当前调用者应先限制来自用户的增长范围，不能把未经验证的接近 `UINT64_MAX` 的大小直接传给 `PGROUNDUP()` 或 `mappages()`。

## 6. 全局内核页表

### 6.1 创建顺序

内核页表的启动顺序是：

```text
CPU 0: kinit()
       -> kvminit()
          -> kvmmake()
             -> kalloc root
             -> kvmmap fixed mappings
             -> proc_mapstacks()
          -> kernel_pagetable = root
       -> kvminithart()
          -> sfence_vma
          -> satp = MAKE_SATP(kernel_pagetable)
          -> sfence_vma

其他 CPU: 等待 started
       -> kvminithart() 使用同一 kernel_pagetable
```

CPU 0 在发布 `started = 1` 前构造完整页表；写入前和非零 CPU 读到标志后的 `__sync_synchronize()`，连同 `started` 的 store/load，组成这里的共享状态发布与等待协议，使其他 CPU 在使用页表前观察到初始化结果。它不是等待所有 hart 到齐的 barrier：CPU 0 发布后立即继续。所有 CPU 共享同一根 `kernel_pagetable`，但各自写自己的 `satp` 和刷新自己的 TLB。

### 6.2 映射清单

`kvmmake()` 创建以下映射：

| 虚拟范围 | 物理范围 | 权限 | 原因 |
|---|---|---|---|
| `UART0 .. UART0+PGSIZE` | 相同 | `R|W` | UART MMIO |
| `VIRTIO0 .. VIRTIO0+PGSIZE` | 相同 | `R|W` | block device MMIO |
| `PLIC .. PLIC+0x4000000` | 相同 | `R|W` | PLIC 寄存器窗口 |
| `KERNBASE .. etext` | 相同 | `R|X` | 内核代码只读、可执行 |
| `etext .. PHYSTOP` | 相同 | `R|W` | rodata/data/bss 及通用 RAM；当前实现没有单独只读 rodata 映射 |
| `TRAMPOLINE` | `trampoline` 的物理页 | `R|X` | 页表切换期间保持同一虚拟 PC |
| 每个 `KSTACK(i)` | 独立 `kalloc()` 页 | `R|W` | 进程槽的内核栈 |

这些映射都没有 `PTE_U`，用户页表也不包含普通内核 RAM。因此用户模式不能访问内核直接映射；`kernmem` 测试会尝试并确认这种隔离。

PLIC 的 64 MiB 映射结束于 UART 区域边界。`kvmmake()` 没有建立从地址 0 开始的普遍恒等映射，只映射实际需要的 MMIO 和 `KERNBASE` 以上 RAM。

### 6.3 内核栈和 guard page

`KSTACK(p)` 定义为：

```c
TRAMPOLINE - ((p + 1) * 2 * PGSIZE)
```

`proc_mapstacks()` 为每个 `NPROC` 槽预先分配一页，并只映射这一页。相邻栈虚拟地址之间留一页无效空洞。栈指针从 `KSTACK(i) + PGSIZE` 开始向低地址增长，因此常规连续越过栈底会触及未映射 guard page，而不是静默写进另一个进程的内核栈。保护只覆盖实际命中该页的访问；一次大跨度地址跳转可以越过整页 guard，若落点另有有效映射，页表硬件不会额外检查“它来自栈溢出”。

内核栈页与进程槽同寿命，不随单次进程退出释放。槽再次使用时复用同一内核栈映射。

### 6.4 trampoline 为什么有高地址别名

用户 trap 刚进入 supervisor 时，硬件仍使用用户页表。`uservec` 必须先保存寄存器，再把 `satp` 切到内核页表；反向返回时也要先切换到用户页表再执行 `sret`。因此 trampoline 的代码页在两种页表中都位于同一个 `TRAMPOLINE` 虚拟地址，切换 `satp` 不会改变正在执行的 PC 所指代码。

同一物理 trampoline 页是内核镜像的一部分，不是 `kalloc()` 页。解除进程页表的 trampoline 映射时绝不能 `kfree()` 它。

### 6.5 生命周期和启动失败

全局内核页表、其中间页表和内核栈在启动后都不释放。`kvmmap()` 把任何映射失败升级为 panic，因为没有可继续运行的退化模式。

`kvmmake()` 对根页表的第一次 `kalloc()` 没有显式判空，随后的 `memset()` 假定启动内存充足。`proc_mapstacks()` 对内核栈 OOM 显式 panic。它们是启动期不可恢复错误，不是面向用户进程的普通失败返回。

## 7. 用户页表生命周期

### 7.1 创建空页表

`uvmcreate()` 分配并清零一页作为 Sv39 根，OOM 时返回 0。`proc_pagetable(p)` 在它之上安装两个 supervisor-only 映射：

1. `TRAMPOLINE -> trampoline`，权限 `R|X`；
2. `TRAPFRAME -> p->trapframe`，权限 `R|W`。

如果映射 trampoline 失败，`uvmfree(root, 0)` 释放根和已创建的中间页表。若 trapframe 映射失败，先用 `uvmunmap(TRAMPOLINE, 1, 0)` 清掉共享代码页映射，再释放页表结构。`p->trapframe` 的物理页仍由 `freeproc()` 负责，不能通过该映射释放。

### 7.2 `exec` 建立并提交新映像

`kexec()` 不在旧页表上就地改写，而是先创建一个私有的新页表：

```text
proc_pagetable(p)
  -> 对每个 ELF LOAD 段 uvmalloc()
  -> loadseg() 把文件内容写入已分配物理页
  -> 分配 guard + USERSTACK 页
  -> uvmclear(guard) 清除 PTE_U
  -> copyout() 放置 argv 字符串与指针数组
  -> 全部成功后：p->pagetable = new, p->sz = new size
  -> proc_freepagetable(old, oldsz)
```

`uvmalloc()` 总会添加 `PTE_R|PTE_U`，再附加 `flags2perm()` 给出的 `PTE_X`/`PTE_W`。因此 ELF text 通常为用户可读可执行，data 为用户可读写；heap 和 stack 由 `PTE_W` 参数建立为用户可读写、不可执行。

guard 页已经占有物理页且 PTE 有效，`uvmclear()` 只清除 `PTE_U`。用户访问它时 `walkaddr()` 失败，`vmfault()` 又因 `ismapped()` 为真而拒绝覆盖，最终进程因权限 fault 被杀死。

只有所有段、栈和参数都成功后才替换 `p->pagetable`。临时页表一旦创建，提交前的后续失败由 `proc_freepagetable(new, sz)` 清理；在创建前失败时则不存在新映像需要释放。旧映像始终仍可用，这是新页表自身的事务式边界，但不表示失败 exec 使旧页表逐位不变：`sys_exec()` 在进入 `kexec()` 前用 `fetchaddr()->copyin()` 从旧页表导入 `argv[]` 指针，可能物化其合法 lazy 页；后续失败不会回滚这些旧地址空间副作用。路径和参数字符串使用 `copyinstr()`，不会以同样方式补页。

### 7.3 eager 增长：`uvmalloc()`

`growproc(n > 0)` 先保证 `p->sz + n <= TRAPFRAME`，然后调用：

```c
uvmalloc(p->pagetable, oldsz, oldsz + n, PTE_W)
```

`uvmalloc()` 从 `PGROUNDUP(oldsz)` 开始逐页：

1. `kalloc()` 取得物理页；
2. 清零整页，保证新用户内存不泄露旧内核数据；
3. 以 `PTE_R|PTE_U|xperm` 安装映射；
4. 最终返回精确的字节大小 `newsz`，而不是向上取整值。

从 `PGROUNDUP(oldsz)` 开始隐含假设：旧 break 所在的部分页已经映射。常规连续 eager 地址空间满足这个不变量；eager/lazy 混用则可能不满足。若先用 `sbrklazy()` 把非页对齐的 `oldsz` 留在未映射页中，随后用 `sbrk()` 正增长，`uvmalloc()` 不会回填该页；当 `newsz <= PGROUNDUP(oldsz)` 时循环为空，越过边界时也只从下一页开始分配。因此“eager”是本次循环内的新页立即分配，不是对历史 lazy hole 的修复保证。

若 `newsz < oldsz`，函数直接返回 `oldsz`。若物理页或中间页表分配失败，它释放本轮已经建立的用户叶页并返回 0；增长前已有映射保持不变。期间建立的空中间页表可能留在仍存活的页表中，最终由 `freewalk()` 回收。

### 7.4 缩减：`uvmdealloc()` 与 `uvmunmap()`

`uvmdealloc(pagetable, oldsz, newsz)` 只在以下条件成立时释放页：

```text
PGROUNDUP(newsz) < PGROUNDUP(oldsz)
```

因此把 break 在同一页内向下移动不会释放该页；包含新末尾字节的页仍然映射。`p->sz` 是字节级逻辑边界，硬件保护则是页级边界，这也是 `sbrklast` 回归测试所覆盖的行为。

`uvmunmap()` 要求起始 VA 页对齐，但与经典 xv6 的严格版本不同，当前实现允许 lazy 空洞：

- 页表路径不存在时跳过；
- 叶 PTE 无效时跳过；
- `do_free != 0` 时先 `kfree(PTE2PA(*pte))`；
- 最后将叶 PTE 清零。

它不回收因此变空的中间页表，也不刷新 TLB。用户内存修改发生在内核页表激活期间，返回用户态时 trampoline 会切换 `satp` 并全量刷新；中间页表则在地址空间销毁时统一回收。

允许空洞只改变每一页的处理结果，不改变遍历次数。`uvmunmap()` 仍从起点到终点逐页调用 `walk()`；因此一次正常缩容的时间复杂度是 `O((PGROUNDUP(oldsz)-PGROUNDUP(newsz))/PGSIZE)`，即使被缩掉的范围只物化了很少几页。lazy allocation 节省物理页，不会让大范围 `uvmdealloc()` 按“实际映射页数”运行。

负数 `sbrk` 和 `sbrklazy` 都走 `growproc()` 的 eager 缩减路径。当前 `growproc()` 对 `sz + n` 的无符号下溢没有单独报错；如果结果绕回到大于等于旧 `sz`，`uvmdealloc()` 会保持旧大小并返回成功。这是当前源码行为，不能把它描述成通用的严格参数校验保证。

### 7.5 `fork`：复制物化页，保留 lazy 空洞

`kfork()` 为子进程建立空页表后调用 `uvmcopy(parent, child, p->sz)`。它按 4 KiB 从 0 扫描到 `sz`：

- 页表路径不存在：跳过，子进程保留相同 lazy 空洞；
- 叶 PTE 无效：跳过；
- 映射存在：为子进程分配新页，复制整页内容和父 PTE 的低位 flags，再安装子映射。

因此当前 `fork` 是完整物理复制，不是 copy-on-write。已物化页在父子进程中有独立物理页；未物化 lazy 页只通过相同的 `sz` 表示，父子以后首次访问时各自分配零页。

分配失败时，`uvmcopy()` 释放此前建立的子叶页并返回 `-1`，随后 `kfork()` 的 `freeproc()` 释放子页表根、中间页和 trapframe。父进程地址空间不变。

一个重要限制是：即使 lazy 地址空间非常稀疏，`uvmcopy()` 仍按 `sz/PGSIZE` 逐页扫描。把 `p->sz` 扩到接近 `TRAPFRAME` 后再 `fork` 会产生很大的线性扫描成本，并不会按实际已映射页数量运行。

### 7.6 销毁

`proc_freepagetable(pagetable, sz)` 的顺序是：

```text
uvmunmap(TRAMPOLINE, 1, do_free=0)
uvmunmap(TRAPFRAME,  1, do_free=0)
uvmfree(pagetable, sz)
  -> uvmunmap([0, PGROUNDUP(sz)), do_free=1)
  -> freewalk(root)
```

高地址两个映射先移除，是因为它们的物理页不由普通用户映像拥有。低地址用户叶页全部解除后，`freewalk()` 才递归释放中间页表和根页。

`uvmfree()` 同样先逐页扫描完整的 `[0, PGROUNDUP(sz))`，所以销毁一个巨大但稀疏的地址空间仍有 `O(sz/PGSIZE)` 的逻辑范围扫描成本，之后还要由 `freewalk()` 扫描每个实际存在的页表页。这个成本出现在父进程 `kwait()->freeproc()` 回收 zombie 时，也出现在成功 `exec` 交换页表后同步释放旧映像时；两条路径都不会因为旧映像只物化了少数页而按少数页运行。

`freewalk()` 看到任何仍有效的叶 PTE 都会 `panic("freewalk: leaf")`。这不是自动清理遗漏叶页的后备方案，而是强制调用者先解除所有叶映射的结构不变量。

`freeproc()` 需要持有 `p->lock`；它先单独释放 `p->trapframe`，再销毁页表。销毁页表时，`TRAPFRAME` 映射随后以 `do_free=0` 清除，所以同一物理页不会释放两次。

## 8. Lazy allocation

### 8.1 本仓库的 API

`kernel/vm.h` 定义：

```c
#define SBRK_EAGER 1
#define SBRK_LAZY  2
```

用户库的 `sbrk(n)` 调用 `sys_sbrk(n, SBRK_EAGER)`，本仓库新增的 `sbrklazy(n)` 调用 `sys_sbrk(n, SBRK_LAZY)`。生成的系统调用 stub 名为 `sys_sbrk`，接受 `n` 和模式两个寄存器参数。

`sys_sbrk()` 的实际策略是：

| 条件 | 行为 |
|---|---|
| `t == SBRK_EAGER` | 调用 `growproc(n)`；正增长立即分配 `uvmalloc()` 从 `PGROUNDUP(oldsz)` 起实际遍历的页 |
| `n < 0` | 无论模式都调用 `growproc(n)`，立即解除越过的页 |
| 其他非负模式 | 只增加 `p->sz`，不分配物理页 |

lazy 正增长先检查 `addr + n < addr` 的无符号溢出，并拒绝超过 `TRAPFRAME`。当前内核没有显式拒绝未知的非 eager 模式值；未知值的非负增长也会被当作 lazy。正常用户 API 只传两个已定义常量。

成功返回增长前的 break，失败返回 `-1`。`n == 0` 不改变地址空间，只返回当前 `p->sz`。

### 8.2 lazy 页的状态

lazy 区域只由逻辑大小表达：

```text
调用 sbrklazy(n)

旧 p->sz -------------------------- 新 p->sz
            没有叶 PTE，也可能连中间页表都不存在

首次访问某一页
            -> kalloc + zero + PTE_R|PTE_W|PTE_U
其余未访问页继续保持空洞
```

所以“地址小于 `p->sz`”并不意味着 `walkaddr()` 一定成功；反过来，缩减后最后一个部分页仍可能映射，页内一小段地址在字节意义上大于等于 `p->sz`，但硬件映射仍存在。

### 8.3 `vmfault()` 的真实步骤

`vmfault(pagetable, va, read)` 当前实现如下：

1. 取得 `p = myproc()`；没有当前进程的上下文不能使用它。
2. 若原始 `va >= p->sz`，返回 0。
3. `va = PGROUNDDOWN(va)`。
4. 若 `ismapped(pagetable, va)` 为真，返回 0；已映射页上的权限错误不能转化为 lazy 分配。
5. `kalloc()` 一页，OOM 返回 0。
6. 清零整页。
7. 在 `p->pagetable` 中以 `PTE_W|PTE_U|PTE_R` 安装映射。
8. 映射失败则释放物理页并返回 0；成功返回物理页基址。

参数 `read` 在当前源码中没有被使用。load fault 和 store fault 都产生用户可读写、不可执行的页。接口保留了这个参数，但当前源码没有提供按访问类型区分权限的保证。

还有一个重要前置条件：函数用参数 `pagetable` 检查是否映射，却始终写入 `myproc()->pagetable`。当前可能触发分配的调用都传当前进程页表；`exec` 对尚未提交的新页表调用 `copyout()` 时，目标 stack 已 eager 映射，因此不会进入 `vmfault()`。如果未来要为任意离线页表处理 fault，必须先消除这个隐含契约。

`vmfault()` 也不记录 heap 起点或“哪些区间由 `sbrklazy()` 预留”。它只要求传入地址低于 `p->sz` 且当前 PTE 无效，因此 `[0, p->sz)` 内任意真正的页洞都可能被补成 `R|W|U` 页。已映射的 text 和 stack guard 依靠 `ismapped()` 阻止权限升级，但当前策略不是按 lazy VMA 精确授权；若引入稀疏 ELF、`mmap` 或不同权限的匿名区间，必须增加区间元数据和权限来源。

### 8.4 用户硬件 page fault 路径

用户态首次 load/store lazy 页的顺序是：

```text
用户 load/store
  -> scause=13(load page fault) 或 15(store page fault)
  -> trampoline uservec 切到 kernel_pagetable
  -> usertrap()
  -> vmfault(p->pagetable, stval, is_load)
  -> 成功：prepare_return -> 切回用户页表 -> 重试原指令
  -> 失败：标记 killed -> kexit(-1)
```

`sepc` 没有为 page fault 加 4，因此返回后硬件重新执行触发 fault 的原指令。新页已经映射并清零，重试才能成功。

“重试成功”还依赖 4.3 节的平台前提：新 lazy PTE 只有 `V/R/W/U`，当前代码没有设置 A/D。硬件更新 A/D 的平台会为翻译补齐所需状态；启用 Svade 且仍把这些位初始化为 0 时，重试会因 A/D 再次 fault，而 `vmfault()` 会因 PTE 已有效而拒绝，除非预先设置适当位或另有独立的软件 A/D handler。

只处理 scause 13 和 15。instruction page fault（scause 12）不会 lazy 分配，且 lazy 页没有 `PTE_X`；跳转到 lazy heap 会走 unexpected trap 并杀死进程。

以下情况 `vmfault()` 返回 0，用户硬件 fault 最终导致进程退出状态 `-1`：

- 地址不小于 `p->sz`；
- 访问 stack guard、只读 text 等已有映射但权限不允许；
- 物理内存耗尽；
- 页表中间页分配失败；
- 目标已经映射，说明这不是一个可由 lazy allocation 修复的缺页。

### 8.5 内核访问 lazy 用户缓冲区

系统调用执行时硬件使用内核页表，用户 VA 通常根本没有内核映射，因此内核不能靠直接解引用触发同样的用户 page fault。`copyin()` 和 `copyout()` 通过软件 walk 发现空洞，并显式调用 `vmfault()`。这让文件读写、pipe 等系统调用能够以 lazy buffer 为参数。

失败语义与用户硬件 fault 不同：`copyin()`/`copyout()` 返回 `-1`，而不是必然杀死进程。上层 file、pipe、console 等调用者可能把它转换为 `-1`、0 或已完成的部分长度；已经复制的数据和已经物化的前置页都不会回滚。

`copyinstr()` 刻意没有调用 `vmfault()`。未物化 lazy 页上的路径字符串会使它返回 `-1`；`lazy_copy` 测试中的 `open()` 用例验证该路径不会导致内核 panic，而不是要求字符串页被自动分配。

## 9. 用户与内核之间的拷贝

### 9.1 为什么不用普通指针

用户运行时使用自己的页表；进入内核后 trampoline 切到 `kernel_pagetable`。内核页表没有按用户 VA 复制每个进程的映射，因此 `memmove(kernel_dst, (void *)user_va, n)` 会访问错误地址或 fault。

三个 copy helper 都先软件遍历用户页表取得物理页基址，再利用内核对 RAM 的恒等映射访问该物理页。

### 9.2 行为对比

| 函数 | 方向 | 长度/终止 | lazy 空洞 | 权限检查 | 返回值 |
|---|---|---|---|---|---|
| `copyout(pt, dstva, src, len)` | 内核 -> 用户 | 精确 `len` | 调用 `vmfault()` 物化 | `walkaddr` 检查 `V|U`，另查 `PTE_W` | 成功 0，失败 -1 |
| `copyin(pt, dst, srcva, len)` | 用户 -> 内核 | 精确 `len` | 调用 `vmfault()` 物化为零页 | `walkaddr` 只检查 `V|U`，不单查 `PTE_R` | 成功 0，失败 -1 |
| `copyinstr(pt, dst, srcva, max)` | 用户 -> 内核字符串 | 遇 NUL 或最多 `max` | 不物化，立即失败 | `walkaddr` 检查 `V|U` | 找到 NUL 为 0，否则 -1 |

`copyin()` 从尚未物化但合法的 lazy 页读取时，会创建一个零页，再把其中的零值字节复制给内核。这是当前实现的明确行为，而不是“读取不存在内存必然报错”。

`copyout()` 在 `walkaddr()` 成功或 `vmfault()` 成功后重新取得 PTE，并拒绝没有 `PTE_W` 的用户 text。它显式拒绝 `va0 >= MAXVA`。`copyin()` 和 `copyinstr()` 通过 `walkaddr()` 的边界检查安全失败；若 `copyin()` 随后调用 `vmfault()`，`p->sz <= TRAPFRAME < MAXVA` 的进程大小约束会先拒绝超高地址。

### 9.3 跨页循环

三个函数都按页处理，核心计算是：

```text
va0 = PGROUNDDOWN(current_va)
n   = min(remaining, PGSIZE - (current_va - va0))
copy physical_page + offset
current_va = va0 + PGSIZE
```

因此未对齐缓冲区和跨多个页面的缓冲区都能工作。每到新页都会重新 walk 和验证。若第二页失败，第一页的拷贝已经生效；接口不提供原子性或回滚。

`copyinstr()` 在每个物理页内逐字节寻找 NUL。只有实际写入 NUL 才返回 0；达到 `max` 或后续页无效时返回 `-1`，目标缓冲区可能包含部分字符串，且失败时不保证以 NUL 终止。调用者只能在返回 0 后使用它作为 C 字符串。

### 9.4 `p->sz` 与映射验证

copy helper 的公开参数只有页表，没有 `sz`。对已经映射的页，它们按 PTE 而不是按 `p->sz` 的每个字节验证。`vmfault()` 仅在需要新建页时检查 `p->sz`，而且 copy helper 传给它的是页首 `va0`。

这造成一个需要明确区分的边界：硬件 fault 传入原始 `stval`，原始地址不小于 `p->sz` 就会失败；`copyin()`/`copyout()` 却传 `PGROUNDDOWN(user_va)`，所以当页首低于 `p->sz` 时，原用户指针即使略微越过 break，copy helper 仍可能先物化整页并访问页内字节。结果是保护粒度保持为页：缩减 break 但未跨页边界后，剩余映射页也仍可被用户和 copy helper 访问。不能把 `p->sz` 解释为硬件级逐字节访问边界。

## 10. 并发、锁和页表发布

### 10.1 锁清单

| 状态 | 保护方式 | 能否睡眠 |
|---|---|---|
| `kmem.freelist` | `kmem.lock` spinlock | 否 |
| 全局 `kernel_pagetable` 内容 | CPU 0 启动期私有构造，发布后只读 | 不适用 |
| 当前进程用户页表内容 | 进程单 hart 执行和生命周期所有权；无专用页表锁 | VM helper 本身不睡眠 |
| 尚未提交的 `exec` 页表 | `kexec()` 当前调用独占 | 分配不睡眠；文件读取可在外层睡眠 |
| 尚未运行的 fork 子页表 | `kfork()` 独占，成功后才设 `RUNNABLE` | VM helper 不睡眠 |

页表函数本身没有内部锁。正确性来自对象尚未发布，或当前进程不可能同时在两个 hart 上运行。`p->lock` 保护进程状态与调度交接，但系统调用期间并不会为了每个 PTE 修改一直持有 `p->lock`。

`copyin()`/`copyout()` 的调用者有时仍持有业务锁：pipe 读写持有 `pi->lock`，`readi()`/`writei()` 可能持有 inode 和 buffer sleeplock，`kwait()` 的 status copyout 持有 `wait_lock` 与子进程 `pp->lock`。lazy 补页会在这些锁内调用 `kalloc()`，锁顺序是“调用者已有锁 -> `kmem.lock`”。当前 VM 路径只短暂获取 `kmem.lock`、不睡眠，且分配器持有 `kmem.lock` 时不会反向获取这些业务锁，所以现有顺序可行。若把 fault 处理扩展为会睡眠、回收页面或获取文件系统锁的实现，必须重新审计这些调用点，不能沿用当前假设。

如果以后实现用户级线程共享页表、多 hart 同地址空间或其他进程修改正在运行的页表，需要新增页表同步、重复 fault 的竞争处理、物理页引用策略以及跨 hart TLB shootdown；当前代码都不提供。

### 10.2 页表修改与 TLB 时机

当前用户 PTE 的建立/删除通常发生在 trap 进入内核、`satp` 已切到 `kernel_pagetable` 之后。用户页表此时不是当前硬件正在查询的页表。返回用户态时：

```text
prepare_return()
  -> 计算 MAKE_SATP(p->pagetable)
  -> trampoline userret
  -> sfence.vma
  -> csrw satp, user_satp
  -> sfence.vma
  -> sret
```

这解释了为什么 `mappages()`/`uvmunmap()` 本身不调用 `sfence_vma()`。这不是说任意场景下修改 PTE 都无需失效 TLB，而是当前调用时机已经由页表切换覆盖。

### 10.3 所有权发布不变量

在 PTE 写入 `PTE_V` 之前，物理页必须已完全初始化：用户数据页清零，复制页完成内容复制，页表页清零。PTE 有效后，页就可能被后续 walk 或硬件观察。

解除 owning 映射时，当前代码先 `kfree(pa)` 再把 PTE 清零。这在现有前提下成立，因为被修改的用户页表未激活且没有并发访问者。若引入并发页表使用者，这个顺序需要重新设计，否则另一执行流可能通过仍有效的 PTE 访问已经回到 freelist 的页。

## 11. 失败、回滚与 panic 边界

| 操作 | 普通失败 | 已完成工作的状态 | panic 条件 |
|---|---|---|---|
| `kalloc()` | 返回 0 | 无 | 无 |
| `kfree()` | 无错误返回 | 合法页被 poison 并入链 | 未对齐、低于 `end`、不低于 `PHYSTOP` |
| `walk(..., 0)` | 路径缺失返回 0 | 无修改 | `va >= MAXVA` |
| `walk(..., 1)` | 中间页 OOM 返回 0 | 早先创建的中间页仍属于页表 | `va >= MAXVA` |
| `mappages()` | walk OOM 返回 -1 | 多页调用时早先映射不自动撤销 | 未对齐、size 0、remap |
| `uvmcreate()` | OOM 返回 0 | 无 | 无 |
| `uvmalloc()` | 返回 0 | 本轮叶页回滚；原地址空间保留；空中间页可保留 | 继承 `mappages` 的可信参数 panic |
| `uvmunmap()` | 空洞直接跳过 | 其他页继续处理 | 起始 VA 未对齐；错误所有权可间接触发 `kfree` panic |
| `uvmcopy()` | 返回 -1 | 已复制的子叶页回滚，父不变；调用者销毁子页表 | 不满足页表结构约束时可能 panic |
| `freewalk()` | 无普通失败 | 递归释放纯页表页 | 仍存在任何有效叶映射 |
| `copyin/out/instr()` | 返回 -1 | 跨页时可能已复制前缀 | 正常恶意用户指针不应导致 panic |
| `vmfault()` | 返回 0 | 映射失败时释放刚取得的页；中间页可能保留 | 依赖当前进程/页表契约被违反时未提供防御 |
| `kvmmap()` | 无错误返回 | 启动期部分映射可能已经存在 | 任意 `mappages` 失败 |

几个容易混淆的边界：

- OOM 是用户地址空间操作可预期的错误，但构造内核页表时是不可恢复启动错误。
- `uvmunmap(..., do_free=0)` 只放弃映射；如果没有其他 owner，物理页会泄漏。`do_free=1` 用在非 owning 共享映射上则会造成悬空映射或双重释放。
- `mappages()` 不增加物理页引用数；复制共享语义必须在更高层实现。
- `freewalk()` 不接受叶页，调用顺序错误会 panic，而不是帮调用者猜测所有权。
- copy helper 的 `-1` 不承诺目标保持原样。

## 12. 可审查的不变量

修改内存代码时至少逐项检查以下陈述：

1. 所有交给 `kfree()` 的地址都页对齐、位于 `[end, PHYSTOP)`，且只释放一次。
2. 所有新用户页在发布 `PTE_V` 前已经清零或被完整、可信的数据覆盖。
3. 有效上层 PTE 只指向页表页；当前实现不允许 superpage 叶。
4. 同一 `(pagetable, va)` 不被 `mappages()` 重映射；替换映射必须先明确解除旧映射和旧页所有权。
5. 普通用户映射都低于 `TRAPFRAME`；`TRAPFRAME`/`TRAMPOLINE` 不带 `PTE_U`。
6. exec 初始 stack guard 的 PTE 有效但无 `PTE_U`；只要该映射尚未被负 `sbrk()` 删除，lazy fault 不得覆盖它。当前实现不保存 guard/heap 下界，删除后再增长可以把原地址建成普通用户页。
7. 销毁页表前先删除所有叶 PTE；只有纯页表树才能交给 `freewalk()`。
8. trampoline 与 trapframe 映射用 `do_free=0`；它们的物理 owner 分别是内核镜像和 `struct proc`。
9. 页表修改发生在该用户页表未被硬件并发使用时；返回用户态前完成相应 TLB 刷新。
10. `vmfault()` 只用于当前进程页表，fault VA 必须小于当前 `p->sz` 且目标 PTE 无效。
11. `copyout()` 必须拒绝只读用户页；`copyinstr()` 失败后的部分缓冲区不能作为字符串使用。
12. eager/lazy 增长都不能越过 `TRAPFRAME`，加法必须先考虑整数溢出。

## 13. 相对经典 xv6 的当前仓库差异

阅读 xv6 book 或其他分支时，不能覆盖掉本仓库的以下事实：

- 用户库同时提供 `sbrk()` 和 `sbrklazy()`，底层系统调用有第二个模式参数。
- `sys_sbrk()` 对 eager 正增长立即分配 `uvmalloc()` 实际遍历的新页，对 lazy 正增长只修改 `p->sz`；非页对齐的历史 lazy hole 不会被后续 eager 增长自动回填，负增长则统一立即解除跨过的页。
- `usertrap()` 处理 load/store page fault，并通过 `vmfault()` 按需分配。
- `uvmunmap()` 容忍不存在的页表路径和无效叶 PTE，以支持稀疏 lazy 地址空间。
- `uvmcopy()` 跳过空洞，使子进程继承逻辑大小但不物化父进程未触碰的 lazy 页。
- `copyin()` 和 `copyout()` 可物化 lazy 页；`copyinstr()` 不会。
- `vmfault()` 的 `read` 参数当前未使用，所有 lazy 页都是 `R|W|U` 且不可执行。
- `mappages()` 要求 `va` 和 `size` 都页对齐；不要照搬允许非对齐 size 的其他版本调用方式。
- 内核通过 `forkret()` 首次执行 `kexec("/init", ...)`，因此第一个进程最初只有 trampoline/trapframe 页表骨架，随后才获得正常用户映像。

## 14. 验证方法

### 14.1 静态检查

从仓库根目录执行：

```sh
make
```

`make` 验证页表宏、函数声明和链接符号一致。还可以检查链接地址：

```sh
riscv64-unknown-elf-nm -n kernel/kernel | grep -E ' (etext|end|trampoline)$'
```

工具链前缀可能由 Makefile 自动选择；若实际前缀不是 `riscv64-unknown-elf-`，应使用 Makefile 检出的对应 `nm`。

预期关系是 `trampoline` 页对齐，`etext` 位于 trampoline 页之后的下一页边界，`end < PHYSTOP`。不要只核对符号存在，还要确认可分配区没有覆盖内核 bss。

### 14.2 正常和边界测试

启动当前默认的 3 hart 配置：

```sh
make qemu CPUS=3
```

在 xv6 shell 中可逐个运行：

```text
usertests copyin
usertests copyout
usertests copyinstr1
usertests copyinstr2
usertests copyinstr3
usertests rwsbrk
usertests sbrkbasic
usertests sbrkmuch
usertests kernmem
usertests MAXVAplus
usertests sbrkfail
usertests sbrkarg
usertests nowrite
usertests pgbug
usertests sbrkbugs
usertests sbrklast
usertests sbrk8000
usertests lazy_alloc
usertests lazy_unmap
usertests lazy_copy
usertests lazy_sbrk
```

每个单项应打印 `test <name>: OK`，最终打印 `ALL TESTS PASSED`。这些测试分别覆盖恶意用户地址、跨页字符串、只读 text、break 边界、OOM 回滚、MAXVA、稀疏分配、解除 lazy 空洞和 copy helper 行为。

`usertests` 驱动在测试前后调用 `countfree()`；若可用页减少，会报告 `FAILED -- lost some free pages`。这对发现 `uvmalloc()`/`uvmcopy()`/`vmfault()` 失败路径泄漏尤其重要。

完整回归与压力验证：

```text
usertests -q
usertests
grind
```

默认多 hart 运行也会持续竞争 `kmem.lock`。若只在 `CPUS=1` 通过而在 `CPUS=3` 失败，应优先检查物理页所有权、双重释放和发布时序。

### 14.3 GDB 观察点

以单 hart 降低噪声：

```sh
make qemu-gdb CPUS=1
```

在 GDB 中适合设置：

```gdb
break kinit
break kalloc
break kfree
break kvmmake
break mappages
break vmfault
break uvmcopy
break freewalk
continue
```

验证 lazy fault 时，在 xv6 shell 运行 `usertests lazy_alloc`，命中 `vmfault` 后检查：

```gdb
p/x va
p/x myproc()->sz
p read
p/x $scause
p/x $stval
```

继续到 `mappages()`，确认 `va` 已页对齐、`perm == PTE_W|PTE_U|PTE_R`，再返回后用 `walk()` 或直接检查对应 PTE 已有 `PTE_V`。load/store fault 返回用户态后应重试同一 `sepc`，而不是跳过指令。

观察 `kfree()` 时确认地址在 `[end, PHYSTOP)` 且页对齐。若同一地址在没有中间 `kalloc()` 的情况下再次进入 `kfree()`，应按双重释放调查，而不能依赖当前范围检查发现它。

### 14.4 失败注入关注点

若为实验临时限制可用物理页，应重点验证：

- `uvmalloc()` 失败后旧进程内容和 `p->sz` 不变；
- `fork()` OOM 返回 `-1`，父进程仍可运行；
- 用户 lazy hardware fault OOM 会杀死该进程但内核继续运行；
- 系统调用中的 `copyin/copyout` OOM 返回错误，不发生 kernel panic；
- 测试结束后 `countfree()` 不低于开始值。

失败注入属于临时源码实验，完成后应恢复实验改动并重新运行完整 `usertests`。仅看到预期失败返回不足以证明正确，还必须检查已经取得的物理页和中间页表最终由谁回收。
