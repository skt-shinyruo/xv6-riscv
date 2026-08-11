# 进程生命周期与调度

本文解释当前仓库的进程表示、创建与回收、父子关系、睡眠与唤醒、每 hart 调度器以及 RISC-V 内核上下文切换。重点是源码实际维持的状态、资源所有权、锁顺序和失败语义，而不是抽象的 POSIX 进程模型。

当前实现有一项必须首先记住的差异：`userinit()` 只创建一个没有用户程序映像的可运行进程；这个进程第一次进入 `forkret()` 后，才在普通进程上下文中执行 `fsinit(ROOTDEV)` 并通过 `kexec("/init", ...)` 装入首个用户程序。它不是经典 xv6 中由 `userinit()` 复制内嵌 `initcode` 的启动方式。

## 1. 范围与源码地图

### 1.1 本文直接覆盖的源码

| 路径 | 核心符号 | 本文关注点 |
|---|---|---|
| `kernel/proc.h` | `struct context`、`struct cpu`、`struct trapframe`、`enum procstate`、`struct proc` | 上下文布局、每 hart 状态、用户寄存器、进程状态与字段锁 |
| `kernel/proc.c` | `allocproc()`、`freeproc()`、`kfork()`、`kexit()`、`kwait()`、`scheduler()`、`sched()`、`sleep()`、`wakeup()`、`forkret()`、`kkill()` | 进程完整生命周期、调度、等待通道和首进程启动 |
| `kernel/swtch.S` | `swtch`、`sd ra`、`ld ra` | 保存旧内核上下文并恢复新内核上下文的汇编 ABI |

阅读本文时应能从 `allocproc()`、`kfork()`、`scheduler()`、`sleep()`、`forkret()` 等 C 入口追到 `struct cpu`、`struct trapframe`、`enum procstate`、`struct proc`，再落到 `swtch` 的 `sd/ld ra` 汇编边界。

### 1.2 关键调用边界

以下路径不由本文独占覆盖，但理解端到端控制流时需要一起阅读：

- `kernel/main.c`：调用 `procinit()`、`userinit()`，最后让每个 hart 进入 `scheduler()`；
- `kernel/vm.c`：`kvmmake()` 调用 `proc_mapstacks()`；`uvmcopy()`、`uvmfree()`、`copyin()` 和 `copyout()` 承接地址空间复制、释放及用户内存访问；
- `kernel/memlayout.h`：定义 `KSTACK(i)`、`TRAPFRAME` 和 `TRAMPOLINE` 的虚拟地址布局；
- `kernel/param.h`：定义 `NPROC`、`NCPU`、`NOFILE` 等固定上限；
- `kernel/spinlock.c`：实现 `p->lock`、`wait_lock` 等锁依赖的关中断和 acquire/release 内存顺序；
- `kernel/trap.c`：timer trap 调用 `yield()`，并在返回用户态前检查 `killed` 后调用 `kexit(-1)`；
- `kernel/sysproc.c`：把 `fork/exit/wait/kill/pause` 系统调用分别接到 `kfork/kexit/kwait/kkill/sleep`；
- `kernel/exec.c`：`kexec()` 替换当前地址空间；首进程在 `forkret()` 中用它装入 `/init`；
- `kernel/trampoline.S`：用 `struct trapframe` 保存和恢复用户寄存器；它不负责 scheduler 的内核上下文切换；
- `kernel/console.c`：`Ctrl-P` 调用 `procdump()` 输出进程表；
- `user/init.c`：首个用户进程；持续创建 shell，并通过 `wait()` 回收 shell 和被收养的孤儿；
- `user/usertests.c`、`user/forktest.c`、`user/grind.c`：进程生命周期、并发和资源耗尽测试；
- `Makefile`：默认创建三个 QEMU hart，提供 `qemu` 和 `qemu-gdb` 调试入口。

地址空间、trap 和系统调用的细节分别见[内存管理](memory.md)、[Trap 与中断](traps-and-interrupts.md)和[系统调用](system-calls.md)；锁 API 的通用语义见[同步、锁与等待通道](synchronization.md)。本文仍会完整说明进程子系统特有的锁交接，因为它是调度正确性的核心。

## 2. 总体模型

当前 xv6 没有独立的线程对象。一个 `struct proc` 同时代表：

1. 一个用户地址空间和一组用户寄存器；
2. 一个内核栈及其暂停时的内核上下文；
3. 文件描述符、当前目录和父子关系等进程资源；
4. 一个可以由任意 hart 调度、但同一时刻最多只能在一个 hart 上运行的调度实体。

系统使用固定大小的全局数组：

```text
proc[NPROC]              NPROC = 64 个进程槽
cpus[NCPU]               NCPU  = 8 个 hart 状态槽
```

进程没有动态分配的 `struct proc`。`allocproc()` 在线性表中把一个 `UNUSED` 槽变成 `USED`；`kwait()` 最终通过 `freeproc()` 把 `ZOMBIE` 槽重新变成 `UNUSED`。因此“进程对象的地址”在槽被复用后仍相同，但代表的逻辑进程已经不同，外部代码不能把裸 `struct proc *` 当作跨生命周期的永久身份。

每个 hart 自己运行一个永不返回的 `scheduler()`。调度器与进程不是两个并行执行者，而是同一个 hart 上轮流使用 CPU 的两套内核上下文：

```text
CPU c 的 scheduler context  <--swtch-->  进程 p 的 kernel context
       c->context                            p->context
```

用户态寄存器不在这两套 `context` 中；用户态进入内核时，它们存入 `p->trapframe`。这是理解 `trapframe` 与 `context` 区别的关键。

## 3. 数据结构、所有权与锁

### 3.1 `struct context`：暂停的内核执行流

`kernel/proc.h` 中的 `struct context` 按 `kernel/swtch.S` 使用的偏移排列：

| 偏移 | 寄存器 | 含义 |
|---:|---|---|
| 0 | `ra` | 恢复上下文后 `ret` 到哪里 |
| 8 | `sp` | 该内核执行流的栈顶位置 |
| 16..104 | `s0..s11` | RISC-V ABI 的 callee-saved 寄存器 |

`swtch()` 不保存 `a0..a7`、`t0..t6` 等 caller-saved 寄存器。调用者按 C ABI 本就必须假定这些寄存器会被调用破坏；需要跨调用存活的值已由编译器保存在内核栈或 callee-saved 寄存器中。`swtch()` 也不保存用户 PC、用户栈指针、`satp` 或特权级。

`gp` 和 `tp` 也不属于进程 context，但两者理由不同。`tp` 有意保存当前 hart id：切入进程时沿用正在执行该切换的 hart 的值，进程以后若被另一个 hart 调度，便应看到新 hart 的 `tp`，而不是恢复上次运行位置的旧值。`gp` 虽然按 psABI 不是每进程状态，当前内核却从未建立可供 C 代码使用的 kernel `gp`：`entry.S` 不初始化它，`uservec` 保存用户 `gp` 后只安装 kernel `sp/tp/satp` 就直接调用 `usertrap()`。因此当前正确性依赖编译和链接结果完全不生成 `gp` 相对的内核 C 访问；“所有执行流共享一个映像”本身不足以建立这个寄存器。当前 `kernel/kernel` 反汇编中的 `gp` 只出现在 `kernelvec` 与 trampoline 的保存/恢复指令，但升级工具链或修改编译选项后必须重查，或显式初始化 kernel `gp`。浮点和向量寄存器同样未保存，但原因不是普通整数调用约定，而是内核根本没有启用和管理相应上下文；平台边界见[平台、ISA 与外部规范契约](../architecture/platform-contracts.md)。

有两类 `context`：

- `p->context`：进程在内核中暂停的位置；首次分配时人为设置为从 `forkret()` 开始；
- `c->context`：该 hart 的 scheduler 在上一次切入进程时暂停的位置。

### 3.2 `struct cpu`：每 hart 状态

每个 `struct cpu` 包含：

| 字段 | 含义与约束 |
|---|---|
| `proc` | 当前 hart 正在运行的进程，scheduler 自身运行时为 0 |
| `context` | scheduler 的暂停上下文，供进程通过 `sched()` 切回 |
| `noff` | 当前 hart 上 `push_off()` 的嵌套深度 |
| `intena` | 最外层 `push_off()` 前中断是否开启 |

`cpuid()` 从 `tp` 读取 hart id。`mycpu()` 直接用该 id 索引 `cpus[]`，所以调用时必须已关闭中断，避免读取期间当前执行流被调度迁移。`myproc()` 用一对 `push_off()/pop_off()` 包住 `mycpu()->proc` 的读取，从而对调用者提供安全快照；返回的进程指针本身并没有因此加引用计数。

`noff/intena` 同时参与 `sched()` 的断言。进程交出 CPU 时只允许留下自己的 `p->lock`，因此 `noff` 必须恰好为 1，而不是只要求“大于零”。

### 3.3 `struct trapframe`：用户态可恢复状态

每个进程分配一整页 `trapframe`，并映射在用户页表的固定虚拟地址 `TRAPFRAME`，但映射没有 `PTE_U`，用户态不能直接访问。它包含：

- 所有用户通用寄存器，包括用户 `sp`、参数/返回值寄存器 `a0..a7`；
- `epc`，即下一次返回用户态的 PC；
- `kernel_satp`、`kernel_sp`、`kernel_trap`、`kernel_hartid`，供下一次从 trampoline 进入内核时建立内核环境。

两个结构不可混淆：

| 问题 | `trapframe` | `context` |
|---|---|---|
| 保存谁 | 用户执行流 | 内核执行流 |
| 何时保存 | user trap 入口 | `swtch()` |
| 寄存器范围 | 几乎全部用户通用寄存器和 `epc` | `ra/sp/s0..s11` |
| 每进程数量 | 一页一个 | `p->context` 一个 |
| scheduler 是否直接读取 | 否 | 是 |

`kfork()` 复制父进程的整个 trapframe，并把子进程的 `a0` 改成 0；这样父进程从 `fork()` 得到子 pid，而子进程从同一次逻辑调用得到 0。

### 3.4 `struct proc` 字段分组

`kernel/proc.h` 已按锁域排列字段。

| 字段 | 所有权/保护规则 | 生命周期含义 |
|---|---|---|
| `lock` | 每槽 spinlock | 保护状态转换及调度交接 |
| `state` | 必须持有 `p->lock` | 当前 `enum procstate` |
| `chan` | 必须持有 `p->lock` | `SLEEPING` 时的等待通道，醒来后清零 |
| `killed` | 必须持有 `p->lock` | 延迟终止请求，不代表已经退出 |
| `xstate` | 必须持有 `p->lock` | `kexit()` 写、父进程 `kwait()` 读的退出状态 |
| `pid` | 修改、槽复用和跨 owner 读取必须持有 `p->lock`；当前进程可依赖自身生命周期稳定读取，`procdump` 是诊断例外 | 当前逻辑进程 id；`UNUSED` 时为 0 |
| `parent` | 必须持有全局 `wait_lock` | 父进程关系；不能只持子进程锁修改 |
| `kstack` | 槽固定，进程私有 | 内核栈的固定虚拟地址；槽释放时不销毁 |
| `sz` | 运行中的进程私有 | 用户地址空间逻辑大小，可能包含延迟分配空洞 |
| `pagetable` | 运行中的进程私有 | 用户页表根 |
| `trapframe` | 运行中的进程私有 | trapframe 物理页的内核地址 |
| `context` | 由 scheduler/当前进程按协议交接 | 暂停的内核上下文 |
| `ofile[NOFILE]` | 进程私有，文件对象有全局引用计数 | 打开的文件描述符 |
| `cwd` | 进程私有，inode 有引用计数 | 当前目录 |
| `name[16]` | 进程私有，调试用途 | 通常由 `exec` 更新 |

“进程私有，不需要 `p->lock`”的前提是调用者确实拥有该运行中进程，或该槽处于由锁排他的构造/销毁阶段。它不授权另一个 hart 在无锁状态下读取正在被 `freeproc()` 清理的字段。

### 3.5 三类核心锁

| 锁 | 保护对象 | 关键规则 |
|---|---|---|
| `pid_lock` | 全局 `nextpid` | `allocpid()` 短暂持有；只保证并发分配不重复 |
| `wait_lock` | 所有 `p->parent` 及 exit/wait/reparent 协议 | 必须在任何相关 `p->lock` 之前获取 |
| `p->lock` | `state/chan/killed/xstate/pid`、调度资格及内核栈排他使用 | scheduler 与进程会跨 `swtch()` 转移持有权 |

进程等待/父子协议中的两条关键顺序是：

```text
wait_lock -> 任意子进程或当前进程的 p->lock
条件锁 lk -> 等待者的 p->lock          （sleep/wakeup 协议）
```

这不是全内核锁图；`allocproc()` 等路径还会从 `p->lock` 获取 allocator、file、inode 或 `pid_lock`。完整调用图见[同步与锁](synchronization.md#111-当前源码的跨模块依赖图)。父子关系路径不能反向持有 `p->lock` 再获取 `wait_lock`。`sched()` 更严格：进入时只能持有当前进程自己的 `p->lock`。

## 4. 初始化与永久内核栈

### 4.1 `proc_mapstacks()`

`kernel/vm.c:kvmmake()` 在构造共享内核页表时调用 `proc_mapstacks(kpgtbl)`。它为 `proc[]` 中每个槽预先分配一个物理页，并映射到：

```text
KSTACK(i) = TRAMPOLINE - (i + 1) * 2 * PGSIZE
```

相邻内核栈之间故意留一个未映射 guard page。内核栈向低地址增长，常规连续增长在越过栈底时会命中该页并触发 page fault，而不是悄悄覆盖相邻进程栈。但它不是完整的栈界限检查：错误代码若一次把地址跳过整页 guard，随后访问更低且恰好已映射的地址，硬件不会把该访问识别为栈溢出。

这些栈的生命周期是整个内核运行期：

- `freeproc()` 不释放内核栈物理页；
- 同一个进程槽被复用时继续使用同一 `kstack` 虚拟地址；
- 任意一次栈页分配失败都会 `panic("kalloc")`，启动过程没有降级或回滚。

这与 trapframe 和用户页不同；后两者每次进程分配/回收都会创建和释放。

### 4.2 `procinit()`

`procinit()` 完成：

1. 初始化 `pid_lock` 和 `wait_lock`；
2. 初始化每个槽的 `p->lock`；
3. 把每个槽设为 `UNUSED`；
4. 按槽下标记录对应的 `p->kstack = KSTACK(i)`。

CPU 0 在发布全局启动完成标志之前执行它。其他 hart 进入 scheduler 时看到的是已经初始化完毕的同一张进程表。

### 4.3 PID 分配

`nextpid` 初始为 1。`allocpid()` 在 `pid_lock` 下取当前值并递增，因此正常系统寿命内并发创建不会得到相同 pid。当前代码没有上界、复用或溢出处理；唯一性保证依赖教学系统不会在 `nextpid == INT_MAX` 时再次调用这条分配路径。把 `INT_MAX-1` 加一并存成 `INT_MAX` 仍可表示，下一次 `nextpid + 1` 才越过范围并在 C 语义中产生未定义行为。这里不能把后续行为简单写成“按二进制回绕”：锁只能串行化更新，不能使溢出成为已定义的 PID 回收协议。

## 5. 完整进程状态机

`enum procstate` 包含六个状态：

```text
UNUSED --allocproc()------------------------------> USED
USED   --userinit()/kfork() 发布------------------> RUNNABLE
USED   --构造失败/freeproc()----------------------> UNUSED
RUNNABLE --scheduler()----------------------------> RUNNING
RUNNING  --yield()--------------------------------> RUNNABLE
RUNNING  --sleep()--------------------------------> SLEEPING
SLEEPING --wakeup()/kkill()-----------------------> RUNNABLE
RUNNING  --kexit()--------------------------------> ZOMBIE
ZOMBIE   --父进程 kwait()/freeproc()--------------> UNUSED
```

详细转换如下：

| 原状态 | 新状态 | 执行者 | 必须持锁 | 含义 |
|---|---|---|---|---|
| `UNUSED` | `USED` | `allocproc()` | 该槽 `p->lock` | 槽已被占用，资源可能仍在分配，scheduler 不可运行它 |
| `USED` | `RUNNABLE` | `userinit()` 或 `kfork()` | 该槽 `p->lock` | 构造完成，正式发布给 scheduler |
| `USED` | `UNUSED` | `allocproc()` 或 `kfork()` 的失败回滚 | 该槽 `p->lock` | 释放已经取得的资源，槽从未对 scheduler 发布 |
| `RUNNABLE` | `RUNNING` | `scheduler()` | 该槽 `p->lock` | 当前 hart 获得该进程的执行权和内核栈使用权 |
| `RUNNING` | `RUNNABLE` | 当前进程 `yield()` | 自己的 `p->lock` | 主动或 timer 抢占，让出一轮 CPU |
| `RUNNING` | `SLEEPING` | 当前进程 `sleep()` | 自己的 `p->lock` | 在 `chan` 上等待条件 |
| `SLEEPING` | `RUNNABLE` | `wakeup()` 或 `kkill()` | 目标 `p->lock` | 允许重新参与调度；不保证立刻运行 |
| `RUNNING` | `ZOMBIE` | 当前进程 `kexit()` | `wait_lock` 后获取自己的 `p->lock` | 用户资源已关闭，保留 pid/status/槽等待父进程回收 |
| `ZOMBIE` | `UNUSED` | 父进程 `kwait()` | `wait_lock` 后获取子 `p->lock` | 释放 trapframe、页表和用户页，槽可复用 |

不存在从 `ZOMBIE` 直接重新运行、从 `SLEEPING` 直接变成 `RUNNING`、或由普通进程自行把自己设为 `RUNNING` 的路径。

### 5.1 各状态的重要不变量

- `UNUSED`：`pid==0`、`pagetable==0`、`trapframe==0`，不应持有文件或 cwd 引用；固定 `kstack` 仍存在。
- `USED`：构造中的私有状态。它不会被 scheduler 选中，也还不一定有 `parent`。
- `RUNNABLE`：构造完整且不在任何 hart 上执行；scheduler 必须先持有 `p->lock` 才能认领。
- `RUNNING`：`c->proc==p` 对对应 hart 成立；同一进程最多有一个 hart 处于此状态。
- `SLEEPING`：`chan` 标识等待条件；它只是内核地址的相等性 token，不会被解引用。
- `ZOMBIE`：进程不会再返回用户态；文件和 cwd 已释放，但 pid、`xstate`、页表、trapframe 及槽仍保留，直到父进程回收。

`p->lock` 不只保护一个枚举值。它把“检查可运行、标记为运行、切入其内核栈”和“停止使用该栈、改变状态、切回 scheduler”串成一个不可分割的所有权协议，从而阻止两个 hart 同时运行同一进程或在旧 hart 尚未离栈时回收该进程。

## 6. 分配、页表与释放

### 6.1 `allocproc()` 的成功契约

`allocproc()` 线性扫描 `proc[]`。对每个槽：获取 `p->lock`，若不是 `UNUSED` 就释放并继续；找到空槽后保持锁并执行：

1. `p->pid = allocpid()`；
2. `p->state = USED`，使其不再被其他分配者认领；
3. `kalloc()` 分配 trapframe 页；
4. `proc_pagetable(p)` 创建空用户页表；
5. 清零 `p->context`；
6. 设置 `p->context.ra = forkret`；
7. 设置 `p->context.sp = p->kstack + PGSIZE`。

成功时返回值非零，并且调用者仍持有返回进程的 `p->lock`。这个返回锁契约非常重要：调用者可以继续构造地址空间和资源，而 scheduler 不可能观察到半成品。

`context.ra` 被设置为 `forkret()` 的原因是新进程从未在内核 C 调用链中执行过，没有可恢复的旧 `sched()` 栈帧。scheduler 第一次加载该 context 后，`swtch()` 末尾的 `ret` 直接进入 `forkret()`。

### 6.2 `proc_pagetable()` 的固定映射

新页表最初没有普通用户内存，只建立两个高地址映射：

1. `TRAMPOLINE` 映射内核的 `trampoline` 代码页，权限为 `PTE_R|PTE_X`，不带 `PTE_U`；
2. `TRAPFRAME` 映射该进程自己的 trapframe 页，权限为 `PTE_R|PTE_W`，同样不带 `PTE_U`。

失败回滚是分层的：

- `uvmcreate()` 失败：直接返回 0；
- trampoline 映射失败：`uvmfree(pagetable, 0)`；
- trapframe 映射失败：先取消 trampoline 映射，再释放空页表。

`proc_freepagetable()` 做相反操作：先取消两个特殊映射，但不在 `uvmunmap()` 中释放它们指向的物理页，再让 `uvmfree()` 释放普通用户页和页表页。trampoline 是全局内核代码，不能由某个进程释放；trapframe 物理页由 `freeproc()` 单独释放。

### 6.3 `freeproc()` 的边界

调用 `freeproc(p)` 时必须持有 `p->lock`。它：

1. 释放 trapframe 物理页并清指针；
2. 用当前 `p->sz` 释放用户页表和普通用户页；
3. 清 `sz/pid/parent/name/chan/killed/xstate`；
4. 最后把状态设为 `UNUSED`。

它刻意不做以下工作：

- 不关闭 `ofile[]`；
- 不对 `cwd` 执行 `iput()`；
- 不释放固定内核栈。

因此它只能用于两个已满足资源前置条件的场景：

- `allocproc()` 或 `kfork()` 在复制文件/cwd 之前失败，此时尚无这些引用；
- `kwait()` 回收 `ZOMBIE`，而 `kexit()` 已经关闭全部文件并释放 cwd。

如果以后在 `kfork()` 中把可能失败的操作移到 `filedup()` 或 `idup()` 之后，就不能仍然无条件只调用 `freeproc()`；必须给新增引用补充回滚。

### 6.4 分配失败资源账本

| 失败点 | 已获得资源 | 当前回滚 | 对调用者结果 |
|---|---|---|---|
| 无 `UNUSED` 槽 | 无 | 逐槽锁均已释放 | `allocproc()` 返回 0，`kfork()` 返回 -1 |
| trapframe `kalloc()` 失败 | pid、`USED` 槽 | `freeproc()` 清槽，然后释放 `p->lock` | 返回 0/-1 |
| `proc_pagetable()` 失败 | trapframe、pid、槽；内部可能有部分页表 | 内部先回滚映射，随后 `freeproc()` 释放 trapframe/槽 | 返回 0/-1 |
| `uvmcopy()` 失败 | 完整空子进程、部分复制的用户页 | `uvmcopy()` 清部分页；`freeproc(np)` 清剩余进程资源并释放锁 | `kfork()` 返回 -1，父进程不变 |
| 启动期内核栈分配失败 | 可能已有前面槽的栈页 | `panic("kalloc")`，不恢复 | 内核停止 |

## 7. 首进程：空地址空间到 `/init`

### 7.1 `userinit()` 实际做了什么

CPU 0 完成设备、buffer、inode 和 file 表初始化后调用 `userinit()`：

1. 调用 `allocproc()`，成功返回时持有首进程锁；
2. 令全局 `initproc = p`；
3. 通过 `namei("/")` 给 `p->cwd` 取得根目录引用；
4. 将状态从 `USED` 改为 `RUNNABLE`；
5. 释放 `p->lock`。

这里没有调用 `uvmalloc()`、没有复制用户指令，也没有设置普通用户入口。此时 `p->sz==0`，页表只有 `TRAMPOLINE` 和 `TRAPFRAME` 两个 supervisor-only 映射。若把它直接返回用户态，用户 PC 并无有效程序可执行。

此时 `fsinit()` 尚未读取 superblock，但 `namei("/")` 仍然可用：绝对路径 `/` 没有需要遍历的路径分量，`namex()` 只通过 `iget(ROOTDEV, ROOTINO)` 在已由 `iinit()` 初始化的 inode cache 中取得引用，不会调用 `ilock()` 读盘。真正读取根 inode 内容和解析 `/init` 发生在 `fsinit()` 完成之后。

`userinit()` 没有检查 `allocproc()` 的空返回；启动期若连首进程槽、trapframe 或页表都无法取得，后续会解引用空指针，而不是走可恢复路径。空指针解引用在 C 层已经是未定义行为，精确异常位置不能只从源码推出。当前 GCC 生成的 `kernel/kernel` 保留空返回于 `s1`，在 `namei("/")` 后用 `sd a0,336(s1)` 写 `p->cwd`；这里已经执行过 `trapinithart()`，且低地址未映射，所以当前产物会以 supervisor fault 进入 `kerneltrap()`，最终得到通用的 `panic("kerneltrap")`，而不是明确的首进程 OOM 诊断。这个当前产物结果不同于更早的 `kvmmake()` 根页表分配失败：后者在 trap 向量安装前就以地址 0 调用 `memset()`，源码没有同样的受控 panic 路径；工具链变化后必须重新核对 `userinit()` 的 UB 产物。`namei("/")` 的这一特殊路径则没有“返回 0”的查找失败：它只调用 `iget()`，有空 inode-cache 槽时返回引用，槽耗尽时直接 `panic("iget: no inodes")`。根 dinode 的磁盘类型直到后续首次 `ilock()` 才验证。

### 7.2 首次 `forkret()`

scheduler 第一次选择首进程时，加载由 `allocproc()` 构造的 context，`swtch()` 的 `ret` 进入 `forkret()`。此时有两个非直觉条件：

- 代码已经在该进程的固定内核栈上；
- `p->lock` 仍由 scheduler 获取并跨切换交给当前执行流。

`forkret()` 首先释放这把锁，然后检查函数内静态变量 `first`。第一次执行时：

```text
fsinit(ROOTDEV)
first = 0
全顺序原子 fence
p->trapframe->a0 = kexec("/init", {"/init", 0})
若返回 -1则 panic("exec")
prepare_return()
跳到 trampoline 的 userret
```

`fsinit()` 必须在普通进程上下文而不是 `main()` 中运行，因为读取磁盘和日志恢复可能调用 `sleep()`；只有已经建立 scheduler/进程 context 后，睡眠才有可切换的目标。

`kexec()` 成功后用 `/init` 的 ELF 页表替换空页表，设置 trapframe 的 `epc/sp/a1`，并返回 `argc`。`forkret()` 把该返回值写入 `a0`，随后 `prepare_return()` 和 trampoline 让 `/init` 从用户入口开始执行。

`first` 是全局一次性标志，不是每进程标志。启动时只有 `initproc` 能运行并首先经过这里；它完成 `/init` 装载后才可能在用户态创建其他进程。因此普通 fork 子进程第一次经过 `forkret()` 时必然看到 `first==0`，跳过 `fsinit()` 和 `/init` 装载。

### 7.3 与常见 xv6 版本的差异

常见教学版本在 `userinit()` 中用 `uvmfirst()` 把一小段内嵌 `initcode` 放到用户地址 0，再由它执行 `exec("/init")`。当前仓库没有这条路径：文件系统初始化和 `kexec("/init")` 都直接发生在首进程的内核态 `forkret()` 中。调试启动问题时若仍在等待 `initcode`、`uvmfirst()` 或首个用户 `ecall exec`，会得到错误结论。

## 8. `kfork()`：复制并发布子进程

用户 `fork()` 经 `kernel/sysproc.c:sys_fork()` 进入 `kfork()`。完整顺序如下：

```text
父进程 p 正在 RUNNING
  -> allocproc()，得到持锁的 USED 子进程 np
  -> uvmcopy(p->pagetable, np->pagetable, p->sz)
  -> np->sz = p->sz
  -> 结构体赋值复制 288 字节 trapframe（不是整页复制）
  -> np->trapframe->a0 = 0
  -> 对每个非空 ofile 调用 filedup()
  -> np->cwd = idup(p->cwd)
  -> 复制 name，记住 pid
  -> release(np->lock)
  -> acquire(wait_lock)，设置 np->parent = p，release(wait_lock)
  -> acquire(np->lock)，设置 RUNNABLE，release(np->lock)
  -> 父进程返回子 pid
```

### 8.1 地址空间与寄存器语义

`uvmcopy()` 为父进程地址范围内的每个有效叶映射分配新物理页、复制内容并保留 PTE flags，其中也包括 exec 用户栈下方清除了 `PTE_U` 的 guard 映射。当前仓库支持 lazy allocation：遇到不存在或无效的 PTE 时 `uvmcopy()` 会跳过，因此子进程保留对应的未分配逻辑空洞；`np->sz` 仍复制父进程逻辑大小。

子进程不是从 `kfork()` 的当前内核栈继续执行。它第一次被调度时进入 `forkret()`，随后根据复制来的 trapframe 返回到父进程 `fork` 系统调用之后。唯一特意修改的用户寄存器是 `a0=0`。父进程的系统调用返回路径则把 `kfork()` 返回的正 pid 写入父 trapframe 的 `a0`。

### 8.2 文件和目录共享

`filedup()` 增加每个 `struct file` 的引用计数，`idup()` 增加 cwd inode 的引用计数。因此 fork 后：

- 父子 `ofile[i]` 指向同一个 open-file 对象，共享文件 offset；
- 父子有各自的描述符数组槽，但关闭一方只减少引用；
- cwd 指向同一 inode 身份，之后各自 `chdir()` 可独立替换自己的 `cwd` 指针。

### 8.3 为什么分三段发布

构造期间 `np->state==USED`，即使暂时释放 `np->lock`，scheduler 也不会运行它。设置父子关系必须持 `wait_lock`，而全局顺序要求 `wait_lock` 在 `np->lock` 之前，所以代码不能保持 `np->lock` 再去获取 `wait_lock`。它采取：

1. 完成私有资源后释放 `np->lock`；
2. 单独在 `wait_lock` 下写 `parent`；
3. 再获取 `np->lock`，最后把状态改成 `RUNNABLE`。

在中间窗口中子进程是完整但不可运行的 `USED`，不会提前退出，也不会被父进程 `kwait()` 当成可回收 zombie。发布 `RUNNABLE` 是构造提交点。

## 9. `kexit()`、`reparent()` 与 `kwait()`

### 9.1 `kexit()` 分阶段释放

`kexit(status)` 只允许当前运行进程调用，且永不返回。若当前进程是 `initproc`，立即 `panic("init exiting")`，因为没有其他进程能承担最终孤儿收养者角色。

退出顺序刻意分成资源清理和状态发布两段：

1. 遍历 `ofile[]`，对每个非空项调用 `fileclose()` 并清槽；
2. 在一笔文件系统事务中对 `cwd` 执行 `iput()`，再将 `cwd=0`；
3. 获取 `wait_lock`；
4. `reparent(p)` 把所有直接子进程交给 `initproc`；
5. `wakeup(p->parent)`，通知可能在 `kwait()` 中睡眠的父进程；
6. 在仍持有 `wait_lock` 时获取自己的 `p->lock`；
7. 写 `p->xstate=status`、`p->state=ZOMBIE`；
8. 释放 `wait_lock`，此时只剩 `p->lock`；
9. 调用 `sched()` 切回 scheduler，永不再执行该进程。

文件关闭和 inode 操作可能获取其他锁或睡眠，因此必须发生在 `wait_lock/p->lock` 之前。到达 `ZOMBIE` 时，这些外部资源已经释放，但页表、trapframe、pid 和退出状态仍必须保留给父进程。

### 9.2 为什么先 `wakeup(parent)` 再写 `ZOMBIE` 仍正确

看起来步骤 5 在步骤 7 之前，似乎父进程可能醒来却看不到 `ZOMBIE`。实际不会丢失，原因是退出进程从步骤 3 到步骤 8 一直持有 `wait_lock`：

- 若父进程正在 `sleep(p, &wait_lock)`，`wakeup()` 把它改成 `RUNNABLE`；它恢复后必须重新获取 `wait_lock` 才能从 `sleep()` 返回并扫描；
- 若父进程尚未睡眠，它同样无法进入持 `wait_lock` 的扫描阶段；
- 退出进程在发布 `ZOMBIE` 后才释放 `wait_lock`，所以父进程下一次扫描必定发生在发布之后。

这里 `wait_lock` 同时是条件锁和父子关系的排序锁。

### 9.3 `reparent()`

`reparent(p)` 要求调用者已持有 `wait_lock`。外层会线性扫描整个进程表，对 `pp->parent==p` 的每个槽：

```text
pp->parent = initproc
wakeup(initproc)
```

修改 `parent` 不需要同时持有 `pp->lock`，因为该字段的唯一保护锁就是 `wait_lock`。这避免了逐层“父锁再子锁”的不稳定树形锁顺序；所有父子关系变更都被一个全局锁串行化。

每收养一个子进程都调用一次 `wakeup(initproc)`；`wakeup()` 自身又扫描并逐个加锁检查整个进程表。若匹配到 `K` 个孩子，总成本是 `O(K * NPROC)`，最坏为 `O(NPROC^2)`，而且整段仍持有全局 `wait_lock`。这在 xv6 的固定小表中可接受，却是扩大 `NPROC` 或批量创建孤儿时必须注意的临界区长度。

被收养的 child 可以仍在运行，也可以已经是尚未回收的 `ZOMBIE`，因为 `reparent()` 不检查状态。每次唤醒只是要求 init 重新扫描；尚无 zombie 时它会再次睡眠，已有 zombie 时则可立即回收。`user/init.c` 的循环会回收 shell，也会忽略退出状态地回收任何被收养进程。

### 9.4 `kwait(addr)`

`kwait()` 在 `wait_lock` 下重复扫描：

1. `havekids=0`；
2. 对每个 `pp`，若 `pp->parent==p`，获取 `pp->lock` 并设置 `havekids=1`；
3. 若子进程为 `ZOMBIE`，保存 pid；可选地把 `xstate` 复制到父进程用户地址 `addr`；
4. `freeproc(pp)` 释放子进程槽；
5. 释放子锁和 `wait_lock`，返回 pid；
6. 若没有孩子或当前父进程已被 kill，释放 `wait_lock` 并返回 -1；
7. 否则执行 `sleep(p, &wait_lock)`，醒来后继续整个循环。

扫描时先用 `wait_lock` 稳定 `parent`，再获取候选子进程的 `pp->lock` 稳定其 `state/xstate`，严格遵守 `wait_lock -> p->lock`。

若 `addr!=0` 且 `copyout()` 失败，`kwait()` 返回 -1，但不会调用 `freeproc()`。子进程继续保持 `ZOMBIE`，父进程可以用有效地址重试。这避免了“状态未交付却已回收”的部分成功，但不表示用户内存完全没变：退出状态固定复制 4 字节，地址若跨页，前 1 至 3 字节可能已经写入；目标是合法 lazy hole 时还可能先物化页。失败重试只保留 zombie，已经发生的用户缓冲区和页表副作用不会回滚。

`kwait()` 一次只回收一个 zombie；有多个退出子进程时由用户态重复调用。没有子进程时不会睡眠，而是立即返回 -1。

找到 zombie 后，`freeproc()` 在仍持有 `wait_lock` 和该 child 的 `p->lock` 时同步销毁地址空间。`uvmfree()` 会逐页扫描 `[0, child->sz)`，即使绝大多数是 lazy hole，因此一次成功 `wait` 的回收阶段可有 `O(child->sz/PGSIZE)` 时间成本；它不睡眠，但会延长这两把锁的持有时间，并推迟其他需要 `wait_lock` 的 fork/exit/wait 关系更新。

### 9.5 为什么 zombie 不会在旧内核栈仍使用时被释放

退出进程设置 `ZOMBIE` 时持有自己的 `p->lock`，并带着该锁执行 `sched()`。父进程可能已经被唤醒，但它回收该子进程前必须获取同一把 `p->lock`，因此会等待。只有 `swtch()` 已把退出进程切回 scheduler、scheduler 不再使用其内核栈并释放该锁后，父进程才能进入 `freeproc()`。

这就是“锁跨 context switch 交接”同时保护状态和内核栈生命周期的具体例子。

## 10. 每 hart scheduler

### 10.1 主循环

每个 hart 从 `kernel/main.c` 进入 `scheduler()` 后不再返回。它先设置 `c->proc=0`，然后永久循环：

1. `intr_on()`，确保上一个进程遗留的关中断状态不会使整个 hart 永久拒绝中断；
2. 紧接着 `intr_off()`，为之后的扫描与可能的 `wfi` 建立无丢失中断窗口；
3. 从 `proc[0]` 到 `proc[NPROC-1]` 线性扫描；
4. 对每个槽获取 `p->lock`；
5. 若 `p->state==RUNNABLE`，设为 `RUNNING`、设置 `c->proc=p`，再 `swtch(&c->context, &p->context)`；
6. 该进程以后切回时，从这次 `swtch()` 返回，清 `c->proc` 并记 `found=1`；
7. 释放 `p->lock`，继续扫描；
8. 整轮没有运行任何进程时执行 `wfi`，等待中断。

先开再关中断不是多余动作。开中断允许处理已有 pending interrupt；扫描和进入 `wfi` 前再关中断，可以避免中断恰好在“确认无可运行进程”之后、`wfi` 之前被处理完，导致 hart 睡过一次唤醒。RISC-V 的 pending interrupt 能让 `wfi` 返回，即使全局中断位暂时关闭；下一轮开中断后再实际处理。

### 10.2 调度策略的实际性质

这是固定槽顺序的全表扫描，不是带显式队列、优先级或严格时间片计数的调度器：

- 每次扫描最多按槽顺序运行每个当时可见的 `RUNNABLE` 进程；
- 进程 `yield()` 后变回 `RUNNABLE`，通常要等下一轮扫描再次经过其槽；
- timer interrupt 在 `kernel/trap.c` 中触发 `yield()`，形成抢占；
- 多 hart 可以同时运行不同进程，靠各 `p->lock` 竞争同一张表；
- 没有严格公平性证明，槽位置和多 hart 竞争会影响实际运行顺序。

### 10.3 调度器与进程的锁交接

切入进程时的时间线：

```text
scheduler                   process p
---------                   ---------
acquire(p->lock)
p->state = RUNNING
c->proc = p
swtch(c.context, p.context)  ---> 恢复在 forkret() 或旧 sched() 之后
                              继承 p->lock
                              ...先在约定位置 release(p->lock)...
```

切回 scheduler 时：

```text
process p                            scheduler
---------                            ---------
acquire/持有 p->lock
state = RUNNABLE/SLEEPING/ZOMBIE
sched()
  swtch(p.context, c.context)  --->  从原 scheduler swtch 返回
                                    继承 p->lock
                                    c->proc = 0
                                    release(p->lock)
```

锁的获取者和释放者可以位于 context switch 两侧，这是 xv6 的特殊协议。这里的“持有者”应理解为当前 hart 上连续的受保护控制流，而不是固定 C 函数。

### 10.4 `sched()` 的四个硬前置条件

`sched()` 在真正切换前逐项检查：

1. 当前执行流必须持有 `p->lock`，否则 `panic("sched p->lock")`；
2. `mycpu()->noff` 必须等于 1，否则 `panic("sched locks")`；
3. `p->state` 不能仍为 `RUNNING`，否则 `panic("sched RUNNING")`；
4. 中断必须关闭，否则 `panic("sched interruptible")`。

第二条说明调用者不能带着任何额外 spinlock 进入 scheduler。第四条防止在 `c->proc`、状态和上下文交接的中途发生可重入调度。

`sched()` 在切换前保存 `mycpu()->intena`，恢复后再写回。原因是“该内核线程在最外层关中断前是否允许中断”属于被暂停执行流的逻辑状态；同一个 hart 的 scheduler 会交错运行不同进程，不能让一个进程的历史污染另一个进程。

### 10.5 `yield()`

`yield()` 是最简单的让出路径：

```text
acquire(p->lock)
p->state = RUNNABLE
sched()
release(p->lock)       // 被重新调度回来后执行
```

timer trap 从用户态或内核态触发时都可能走到 `yield()`。进程恢复后，`sched()` 返回时仍持有自己的锁，所以由 `yield()` 释放。它只保证让出当前 CPU，不保证其他特定进程一定先运行。

## 11. `swtch.S`：上下文切换的精确边界

汇编接口为：

```c
void swtch(struct context *old, struct context *new);
```

RISC-V 调用约定把 `old` 放在 `a0`、`new` 放在 `a1`。`kernel/swtch.S` 的 `swtch:` 标签先执行一组 `sd`：

```text
sd ra,   0(a0)
sd sp,   8(a0)
sd s0,  16(a0)
...
sd s11,104(a0)
```

然后用对应的 `ld ra`、`ld sp`、`ld s0..s11` 从 `a1` 恢复新 context，最后 `ret`。

`ret` 等价于跳到刚加载的 `ra`：

- 从 scheduler 切入从未运行的新进程时，新 `ra` 是 `allocproc()` 写入的 `forkret`；
- 从 scheduler 切入暂停进程时，新 `ra` 是它上次调用 `swtch()` 后应继续的位置，即 `sched()` 内调用点之后；
- 从进程切回 scheduler 时，新 `ra` 是该 hart 上次在 `scheduler()` 中调用 `swtch()` 后的位置。

`sp` 与 `ra` 一起让普通 C 调用栈看起来像“很久以后同一次函数调用终于返回”。没有复制栈，也没有从一个栈 unwind 到另一个栈；只是更换栈指针。

`swtch()` 不执行以下操作：

- 不修改进程状态；调用者必须在切出前先修改；
- 不获取或释放任何锁；锁交接由 C 层协议保证；
- 不切换用户页表；scheduler 与进程内核代码都在共享 kernel page table 上运行；
- 不进入/退出用户态；那是 trap/trampoline 路径；
- 不选择下一个进程；那是 `scheduler()` 的线性扫描。

## 12. `sleep()` / `wakeup()` 与丢失唤醒证明

### 12.1 API 契约

调用者必须：

1. 持有保护“等待条件”的 spinlock `lk`；
2. 在循环中检查条件，而不是把一次唤醒当作条件成立；
3. 传入一个在等待期间保持可比较身份的 `chan`；
4. 不把当前进程自己的 `p->lock` 作为 `lk`，因为当前实现会再次获取 `p->lock`，导致递归获取 panic。

典型调用形式是：

```c
acquire(&lk);
while (!condition) {
  if (killed(myproc())) {
    // 按该子系统语义退出
  }
  sleep(chan, &lk);
}
// 此处仍持有 lk，重新确认并消费 condition
release(&lk);
```

唤醒方应在同一把条件锁下先更新条件，再调用 `wakeup(chan)`。

### 12.2 `sleep()` 的原子锁交接

`sleep(chan, lk)` 的实际顺序是：

```text
调用者已持有 lk
  -> acquire(p->lock)
  -> release(lk)
  -> p->chan = chan
  -> p->state = SLEEPING
  -> sched()                    # 带 p->lock 切回 scheduler
  ...被 wakeup/kill 改为 RUNNABLE并重新调度...
  -> sched() 返回，仍持有 p->lock
  -> p->chan = 0
  -> release(p->lock)
  -> acquire(lk)
  -> 返回调用者，此时重新持有 lk
```

它不是“先释放条件锁，再尝试登记睡眠”，而是用 `p->lock` 接替条件锁覆盖临界窗口。

### 12.3 为什么不会丢失唤醒

设睡眠者 S 持有条件锁 `lk`，唤醒者 W 也遵守“持 `lk` 更新条件并 wakeup”的协议。

只有两类顺序：

1. W 在 S 获取 `p->lock` 之前尝试更新条件。此时 S 仍持有 `lk`，W 无法越过条件锁；S 会先获得 `p->lock`，然后才释放 `lk`。
2. W 在 S 释放 `lk` 之后获得条件锁。此时 S 已持有 `p->lock`；W 的 `wakeup()` 必须获取同一个 `p->lock` 才能检查状态，因此会等到 S 已写好 `chan/SLEEPING` 并通过 `sched()` 把锁交给 scheduler 后再继续。W 随后必定看到可唤醒状态并改成 `RUNNABLE`。

spinlock 的 acquire/release 内存顺序还保证 `chan/state` 的写入对另一个 hart 可见。因此不存在“条件已经成立并完成 wakeup，但 S 之后才发布 SLEEPING”的空窗。

如果唤醒者不持条件锁，或睡眠者在获取 `p->lock` 之前自行释放条件锁，这个证明不成立。

### 12.4 `wakeup()` 的行为

`wakeup(chan)` 线性扫描所有进程，对除当前进程外的每个槽获取 `p->lock`。匹配条件：

```text
p->state == SLEEPING && p->chan == chan
```

所有匹配者都被改为 `RUNNABLE`，所以这是 broadcast，不是唤醒一个 waiter。被唤醒只表示有资格参与调度：多个进程争用同一资源时，先运行者可能消费条件，其他进程必须在条件锁下重新检查并再次睡眠。

`chan` 只是指针值。例如 `kwait()` 以父进程指针 `p` 为通道，timer pause 以 `&ticks` 为通道，pipe 和磁盘使用各自状态对象地址。通道对象在 waiter 存活期间必须有稳定地址，不能用已经离开作用域的临时对象制造身份。

### 12.5 wait 的特例

`kwait()` 持有 `wait_lock` 检查“是否有 zombie 子进程”，然后调用 `sleep(p, &wait_lock)`。子进程 `kexit()` 也在 `wait_lock` 下调用 `wakeup(p->parent)`。因此普通条件锁证明直接适用：父进程不会错过子进程退出，也不会在没有孩子时无条件睡眠。

这项证明只覆盖 child 状态变化，不覆盖 kill 取消。完整扫描后 `kwait()` 在持 `wait_lock` 时调用 `killed(p)`，但 `kkill()` 不取得 `wait_lock`；从这次检查返回到 `sleep()` 取得 `p->lock` 之间仍有取消窗口。此时 kill 只置标志而看不到 `SLEEPING`，父进程随后可一直睡到某个 child 退出或第二次 kill 到来。它醒来后会重扫并返回 `-1`，但第一次 kill 本身不保证立即解除这次尚未发布的 wait；父进程自己被 reparent 并不是这里的唤醒来源，因为单线程进程不可能同时在 `kwait()` 睡眠又执行自己的退出路径。

## 13. Kill 是延迟终止请求

### 13.1 `kkill(pid)`

`kkill()` 线性扫描进程表，逐槽持有 `p->lock` 比较 pid。找到后：

1. 设置 `p->killed=1`；
2. 若目标正处于 `SLEEPING`，改成 `RUNNABLE`，使其有机会离开睡眠路径；
3. 释放锁并返回 0。

找不到返回 -1。它不直接释放目标资源，不直接把状态改为 `ZOMBIE`，也不会在另一个 hart 的内核栈上强行执行退出。

`setkilled(p)` 在持有目标锁时置位，`killed(p)` 在持锁时读取。`kernel/trap.c:usertrap()` 在系统调用前及处理 trap 后检查标志并调用 `kexit(-1)`。因此：

- 用户态 CPU-bound 进程会在 timer trap 后看到 kill；
- 正在系统调用中的进程通常完成到安全返回点后退出；
- 睡眠进程被置为 `RUNNABLE` 后，具体子系统应在循环中检查 `killed()` 或完成不可取消操作，再走到 trap 返回检查；
- kill 的可观察退出状态为 -1，`usertests killstatus` 验证该语义。

### 13.2 当前实现的边界

- `kexit()` 禁止 init 退出，但 `kkill()` 本身没有拒绝 `initproc`；向 init pid 发 kill 最终可能在退出检查处触发 `panic("init exiting")`。用户程序必须把 init 视为不可杀死的系统进程。
- 合法 pid 来自 `kfork()`，从 1 开始。`kkill()` 没有显式拒绝 `pid<=0`，也没有先检查 `state!=UNUSED`；由于空槽的 pid 为 0，`kill(0)` 可能匹配一个 `UNUSED` 槽并返回成功，还会在该空槽留下 `killed=1`。`allocproc()` 认领 `UNUSED` 槽时不会主动清该字段，因此下一次复用该槽的进程还可能继承这次错误置位。当前接口应只传正的已知 pid；若要提供健壮的公共语义，源码需要增加校验并配套测试，文档不能假定它已经存在。
- 被 kill 的进程仍需由父进程 `wait()` 回收。kill 不等于 wait。

## 14. `proc.c` 中的其他入口

### 14.1 `growproc(n)`

`growproc()` 修改当前进程地址空间和 `p->sz`：正数调用 `uvmalloc()` 并拒绝越过 `TRAPFRAME`；负数调用 `uvmdealloc()`；成功后提交新大小，失败保持旧 `sz`。普通 `sbrk` 的 eager 路径使用它；lazy 路径在 `kernel/sysproc.c` 中只增加 `sz`，由 page fault 后续分配。

它没有获取 `p->lock`，因为只能由当前运行进程修改自己的地址空间。其他 hart 不应并行操作该进程的页表。

### 14.2 `either_copyout()` / `either_copyin()`

这两个适配器让 pipe/console 等公共 I/O 代码同时支持用户缓冲区和内核缓冲区：

- `user_dst/user_src` 为真时使用当前 `p->pagetable` 调用 `copyout/copyin`；
- 为假时使用 `memmove()` 直接访问内核地址。

它们不改变进程状态，但依赖存在当前进程。用户拷贝失败向调用者返回 -1，不会在这里 kill 进程。

### 14.3 `procdump()`

控制台收到 `Ctrl-P` 时调用 `procdump()`，打印非 `UNUSED` 槽的 pid、状态和名称。它故意不获取 `p->lock`：调试死锁时，等待进程锁会让诊断功能也卡住。代价是输出只是一张可能不一致的瞬时快照，不能据此证明并发不变量被破坏。

状态输出缩写包括 `used`、`sleep`、`runble`、`run` 和 `zombie`。未知枚举打印 `???`。

## 15. 核心并发不变量

修改进程代码时应逐条检查以下陈述：

1. CPU 0 在其他 hart 和 scheduler 尚不可运行的 `procinit()` 启动阶段可以无锁建立每个槽的初始 `state=UNUSED`；此后只有持有目标 `p->lock` 的代码才能改变 `state/chan/killed/xstate/pid`。拥有自身执行权的当前进程可无锁读取生命周期稳定的 `pid` 等私有状态；另一个 hart 不能据此读取正在回收的槽。`procdump()` 的无锁只读是有意的诊断折中。
2. 已发布进程的 `parent` 读取或写入都在 `wait_lock` 下；父子关系不由 `p->lock` 保护。唯一受控例外是 `freeproc()` 回滚尚未发布、`parent` 仍为 0 的构造失败槽；正常 wait 回收时调用者同时持有 `wait_lock`。
3. `wait_lock` 总是在任意 `p->lock` 之前获取。
4. 进程进入 `sched()` 前已把状态改为非 `RUNNING`，只持有自己的 `p->lock`，且中断关闭。
5. scheduler 只有在持有候选锁时才能把 `RUNNABLE` 改成 `RUNNING` 并使用其内核栈。
6. 进程从 `sched()` 恢复或首次进入 `forkret()` 时继承 `p->lock`，必须按对应路径释放。
7. `sleep()` 返回时重新持有调用者传入的条件锁；调用者必须用循环重新检查条件。
8. 唤醒者在保护条件的同一把锁下先更新条件，再 `wakeup()`。
9. 只有已经关闭文件和 cwd 的 zombie，或尚未复制这些引用的构造失败进程，才可以交给 `freeproc()`。
10. `RUNNABLE` 是构造发布点；在 `parent` 和共享资源建立完成之前，子进程保持 `USED`。
11. `ZOMBIE` 是退出发布点；父进程在取得子锁前不能释放页表和 trapframe。
12. 同一 `struct proc` 地址在槽复用后不代表同一逻辑进程；需要身份判断时至少结合锁下 pid 和状态。

## 16. 常见错误与失败表现

| 修改错误 | 可能表现 | 首先检查 |
|---|---|---|
| 带额外 spinlock 调用 `sched()` | `panic: sched locks` | `mycpu()->noff`、调用路径持锁集合 |
| 状态仍为 `RUNNING` 就切出 | `panic: sched RUNNING` | `yield/sleep/exit` 是否先改状态 |
| 未持 `p->lock` 调用 `sched()` | `panic: sched p->lock` | 锁是否提前释放 |
| 中断开启时切换 | `panic: sched interruptible` | `push_off/pop_off` 是否失配 |
| 先释放条件锁再获取 `p->lock` | 偶发永久睡眠 | sleep/wakeup 丢失唤醒窗口 |
| 用 `if` 而非 `while` 检查条件 | 广播唤醒后读到空资源 | 条件循环和锁范围 |
| `p->lock -> wait_lock` 反序 | exit/wait/reparent 多核死锁 | `reparent2` 测试、锁栈 |
| 提前设 `RUNNABLE` | 子进程看到缺失 parent/文件/cwd | fork 发布顺序 |
| zombie 尚未离开内核栈就释放 | 页表/栈破坏、随机 trap | 是否绕过子 `p->lock` |
| 构造失败后遗漏 `freeproc()` | 进程槽或物理页泄漏 | `forktest`、`usertests` free-page 检查 |
| exit 跳过 `fileclose/iput` | file/inode 引用泄漏 | zombie 资源账本 |
| 把 `trapframe` 当 `context` | 返回地址或寄存器解释错误 | 当前切换是用户/内核还是内核/scheduler |

## 17. 典型端到端流程

### 17.1 普通 fork 子进程第一次运行

```text
父用户态 fork()
  -> user ecall / syscall dispatch
  -> sys_fork()
  -> kfork()
       allocproc(): USED，context.ra=forkret
       复制页表、trapframe、文件、cwd
       设置 parent
       发布 RUNNABLE
  -> 父进程从 syscall 返回，a0=child_pid

某 hart 的 scheduler
  -> 子 RUNNABLE -> RUNNING
  -> swtch(c->context, child->context)
  -> forkret() 释放 child->lock
  -> first 已为 0，跳过 fsinit/kexec
  -> prepare_return() + trampoline userret
  -> 子用户态从 fork 后继续，a0=0
```

### 17.2 timer 抢占并恢复

```text
用户进程 RUNNING
  -> timer trap 进入 kernel/trap.c
  -> yield()
       acquire p->lock
       RUNNING -> RUNNABLE
       sched(): 保存 p->context，恢复 c->context
  -> scheduler 清 c->proc、释放 p->lock
  -> 以后某轮再次选中 p，RUNNABLE -> RUNNING
  -> swtch 恢复 p->context
  -> 原 sched() 返回，yield() 释放 p->lock
  -> trap 路径恢复 CSR，返回用户态
```

### 17.3 子进程退出、父进程已在 wait 中

```text
父：持 wait_lock 扫描，无 zombie
父：sleep(parent_pointer, &wait_lock)
    发布 SLEEPING 并切回 scheduler

子：关闭文件和 cwd
子：持 wait_lock，reparent 自己的孩子
子：wakeup(parent_pointer)，父 SLEEPING -> RUNNABLE
子：持自身锁写 xstate 和 ZOMBIE
子：释放 wait_lock，sched()，仍持自身锁
子所在 scheduler：切回后释放子锁

父：重新运行，从 sleep 恢复并重新获取 wait_lock
父：扫描到 ZOMBIE，获取子锁，copyout status
父：freeproc，ZOMBIE -> UNUSED，返回子 pid
```

### 17.4 父进程先退出

```text
父 kexit()
  -> wait_lock
  -> reparent：每个 child.parent = initproc
  -> wakeup(initproc)
  -> 父自己进入 ZOMBIE

子以后 kexit()
  -> wakeup(initproc)
  -> init 的 wait() 回收该子

原父也由它自己的父进程回收；若原父本身也是孤儿，则同样由 init 回收。
```

## 18. 验证与调试

### 18.1 静态验证

从仓库根目录运行：

```sh
make kernel/kernel
```

该命令确保 C/汇编布局和符号能成功编译链接。

可以检查 `struct context` 与汇编偏移是否同步：

```sh
sed -n '1,45p' kernel/proc.h
sed -n '1,80p' kernel/swtch.S
```

若在 `struct context` 中插入、删除或重排字段，必须同步修改 `swtch.S` 的每个 `sd/ld` 偏移，否则编译器通常不会替你发现 ABI 损坏。

### 18.2 功能和竞态测试

启动单 hart 与默认多 hart 两种配置：

```sh
make qemu CPUS=1
make qemu
```

在 xv6 shell 中按需运行：

```text
usertests exitwait
usertests reparent
usertests reparent2
usertests twochildren
usertests forkfork
usertests forkforkfork
usertests preempt
usertests killstatus
usertests forktest
forktest
usertests -q
grind
```

各测试的主要目标：

| 测试 | 主要覆盖 |
|---|---|
| `exitwait` | 退出状态交付、exit/wait 竞态 |
| `reparent` | 活子进程在父退出时被 init 收养 |
| `reparent2` | `wait_lock -> p->lock` 顺序及历史 deadlock/release 回归 |
| `twochildren` | 两个孩子并发退出和父进程重复 wait |
| `forkfork` / `forkforkfork` | 并发分配、pid/进程槽耗尽和恢复 |
| `preempt` | timer 抢占、多个忙循环与 pipe 唤醒推进 |
| `killstatus` | kill 后最终 `kexit(-1)` 及 wait 状态 |
| `forktest` | `allocproc/uvmcopy` 失败能否干净返回、槽能否回收 |
| `usertests -q` | 全套快速回归，并比较前后空闲页数发现泄漏 |
| `grind` | 低概率 fork/exit/wait/kill、内存、pipe、文件组合竞态 |

多 hart 测试是必要的：`CPUS=1` 适合复现确定性控制流，却无法覆盖两个 scheduler 同时扫描、exit/wait 真正并发等问题。

### 18.3 运行时观察

QEMU console 中按 `Ctrl-P` 可触发 `procdump()`。预期可看到：

- shell 等待输入时通常是 `sleep`；
- CPU-bound 测试在 `run` 与 `runble` 间变化；
- 未被父进程及时 wait 的退出进程显示 `zombie`。

输出不加进程锁，只适合定位线索，不是原子快照。

### 18.4 GDB 断点

终端一启动等待调试的 QEMU：

```sh
make qemu-gdb
```

按 [VSCode/GDB 调试说明](../build/vscode-debug.md)连接后，可设置以下符号断点：

```gdb
break userinit
break allocproc
break kfork
break forkret
break scheduler
break sched
break swtch
break sleep
break wakeup
break kexit
break reparent
break kwait
break kkill
```

建议按问题选择少量断点，`scheduler/swtch/wakeup` 在多 hart 下命中极其频繁。常用观察命令：

```gdb
info threads
thread apply all bt
p *p
p p->state
p p->pid
p/x p->chan
p cpus
p proc[0]
x/14gx $a0
x/14gx $a1
```

在 `swtch` 入口，`$a0` 是待保存 context，`$a1` 是待恢复 context；`x/14gx` 可以核对 `ra/sp/s0..s11`。在 `forkret` 首次命中时检查 `p->sz==0`、页表只有特殊映射的启动状态，单步越过 `kexec()` 后再检查 `p->trapframe->epc/sp/a0/a1`。

在 `sched()` 断点检查：

```gdb
p mycpu()->noff
p myproc()->state
p myproc()->lock
p/x $sstatus
```

预期满足 `noff==1`、状态不是 `RUNNING`、当前 hart 持有进程锁，且 `$sstatus` 的 `SSTATUS_SIE` 位（位 1）为 0。`intr_get()` 是 `kernel/riscv.h` 中的 `static inline`，优化构建下 GDB 未必能把它当作可调用函数，因此直接观察 CSR 更可靠。局部变量也可能显示为 optimized out；这时在函数入口后单步数条源码语句，或直接从 `myproc()` 和全局 `proc[]/cpus[]` 观察。

### 18.5 修改后的最低验收

进程或调度代码变更至少应满足：

1. `make kernel/kernel` 成功；
2. `CPUS=1` 下目标测试通过；
3. 默认 `CPUS=3` 下 `exitwait/reparent2/preempt/forktest` 通过；
4. `usertests -q` 前后无空闲页泄漏报告；
5. 涉及低概率锁交接时运行 `grind`；
6. 状态、锁顺序、失败回滚或首进程路径改变时同步更新本文。

## 19. 阅读检查表

读完本文后，应能仅依靠源码回答：

- 为什么 `allocproc()` 成功时返回一个仍被锁住的 `USED` 进程？
- 为什么 `kfork()` 要在设置 `parent` 前释放子进程锁，并在最后重新获取它发布 `RUNNABLE`？
- 为什么 `kexit()` 唤醒父进程发生在写 `ZOMBIE` 之前仍不会丢失事件？
- 为什么 `sched()` 要求 `noff==1` 而不是只要求中断关闭？
- 为什么 scheduler 获取的 `p->lock` 要由另一个 context 中的 `forkret()` 或恢复进程释放？
- 为什么 `swtch.S` 只保存 `ra/sp/s0..s11` 就足以恢复内核 C 执行流？
- 为什么 `sleep()` 必须先拿 `p->lock` 再释放条件锁？
- 为什么 kill 只能请求目标在安全点退出，而不能立即 `freeproc()`？
- 为什么当前仓库首进程的 `forkret()` 会执行 `fsinit()` 和 `kexec("/init")`，普通子进程却不会？
- 为什么内核栈随进程槽永久存在，而 trapframe 和用户页要到 wait 回收时才释放？

这些问题的答案共同构成当前进程子系统的核心不变量。
