# `swtch.S`：保存和恢复内核 continuation

`swtch(struct context *old, struct context *new)` 不是 trap 入口，也不是完整的处理器快照。它把当前内核 C continuation 所需的 ABI 状态写入 `old`，从 `new` 取出另一条 continuation，再用该 continuation 的 `ra` 和 `sp` 继续执行。源码只有 29 条指令：14 条 `sd`、14 条 `ld` 和一条 `ret`。

## 1. C 调用点和锁契约

当前只有两个切换方向：

```text
scheduler: swtch(&c->context, &p->context)
process:   swtch(&p->context, &mycpu()->context)  // 经 sched()
```

`scheduler()` 取得 `p->lock` 后，把 `p->state` 置为 `RUNNING`、把 `c->proc` 置为 `p`，再切入进程。进程经 `sched()` 切回前必须满足：

- 持有 `p->lock`；
- 已把 `p->state` 改成非 `RUNNING`；
- `sstatus.SIE==0`；
- `mycpu()->noff==1`。

源码注释中的 “Must hold only `p->lock`” 只约束**自旋锁**。`noff` 是 `push_off()` 的嵌套深度，标准 `acquire()` 用它跟踪自旋锁并关闭中断；它不是完整的锁清单，也不记录 sleeplock。进程可以在 continuation 中逻辑持有 inode 或 buffer 的 sleeplock，因等待 I/O 或另一个条件而经过 `sleep()`/`sched()`，随后在同一进程恢复时继续持有它。sleeplock 的内部自旋锁在睡眠前已经释放，sleeplock 的逻辑所有者仍由进程身份表示。

`swtch.S` 自身不检查上述条件，也不读写 `p->state`、`c->proc`、`noff` 或任何锁。检查和所有权协议都在 `scheduler()`、`sched()` 及其调用者中。

## 2. `struct context` 的硬编码 ABI

| 偏移 | 字段 | ABI 作用 |
|---:|---|---|
| 0 | `ra` | continuation 的恢复 PC，最终由 `ret` 消费 |
| 8 | `sp` | continuation 的内核栈位置 |
| 16 | `s0` | callee-saved |
| 24 | `s1` | callee-saved |
| 32 | `s2` | callee-saved |
| 40 | `s3` | callee-saved |
| 48 | `s4` | callee-saved |
| 56 | `s5` | callee-saved |
| 64 | `s6` | callee-saved |
| 72 | `s7` | callee-saved |
| 80 | `s8` | callee-saved |
| 88 | `s9` | callee-saved |
| 96 | `s10` | callee-saved |
| 104 | `s11` | callee-saved |

结构总大小是 112 字节。汇编直接编码这些偏移；调整 `kernel/proc.h` 中的顺序、类型或对齐而不同步修改 `kernel/swtch.S`，会把错误推迟到运行时。适合增加的构建期约束是 `sizeof(struct context)==112`，以及每个字段的 `offsetof` 断言。

保存集合之所以足够，依赖 RISC-V C ABI：

- caller 已接受 `a0..a7`、`t0..t6` 和 `ra` 可被普通调用破坏；仍需跨调用存活的值由编译器放入 callee-saved 寄存器或当前栈帧；
- `sp`、`s0..s11` 必须在 C 调用返回时恢复，`ra` 还承担 continuation 的恢复 PC，因此汇编保存它们；
- `gp` 不被这段汇编改写；`tp` 也不改写，它必须继续表示**执行当前物理切换的 hart**，不能从可迁移的进程 context 恢复；
- 用户 GPR 在 trapframe 中，CSR 属于 trap/每 hart 状态，都不在 `struct context` 中。

若手写汇编 caller 在调用点保留未 spill 的 caller-saved 活跃值，或内核开始使用需要跨调度保存的浮点/向量寄存器，这个证明即失效。

## 3. 两类内核栈的完整布局

### 3.1 每 hart 的 scheduler `stack0`

`kernel/start.c` 在 `.bss` 中定义一个 16 字节对齐的连续数组：

```text
stack0[4096 * NCPU]

低地址  stack0 + h*4096       hart h 的栈段下界
        ...                    C 栈向低地址增长
高地址  stack0 + (h+1)*4096   entry.S 设置的初始 sp
```

`entry.S` 在分页开启前根据 `mhartid` 选择这一段。`start()` 经 `mret` 进入 `main()`，`main()` 最终调用不返回的 `scheduler()`；这些启动和调度 C 栈帧都留在相应 hart 的 `stack0` 段上。`swtch(&c->context, ...)` 保存的 `c->context.sp` 就指向该 hart 当时的 scheduler 栈帧。

`stack0` 各段彼此相邻，没有专门的无效 guard page；分页开启后它作为内核数据被直接映射。scheduler continuation 属于 `struct cpu`，因此固定在该 hart 及其 `stack0` 上，不会迁移。

### 3.2 每进程内核栈和 guard page

`proc_mapstacks()` 在创建内核页表时为每个 `proc[i]` 分配一个物理页，并以 `PTE_R|PTE_W` 映射到：

```text
base(i) = KSTACK(i) = TRAMPOLINE - (i + 1) * 2 * PGSIZE

高地址  base(i) + PGSIZE      新进程的初始 context.sp
        mapped stack page      [base(i), base(i)+PGSIZE)
        ...                    栈向低地址增长
低地址  base(i)
        invalid guard page     [base(i)-PGSIZE, base(i))
```

相邻进程栈之间相隔一个未映射页。guard page 能让通常的向下栈溢出触发 supervisor page fault；它不是 `stack0` 的保护机制。进程栈映射存在于所有 hart 共用的内核页表中，不映射进该进程的用户页表。

`allocproc()` 把整个 `context` 清零，然后设置：

```text
p->context.ra = forkret
p->context.sp = p->kstack + PGSIZE
```

第一次调入时没有从栈中弹出合成返回地址；`ret` 直接使用 context 中的 `forkret`。进入 `forkret()` 后，C prologue 才从页顶向下建立第一个栈帧。后续切出时，`context.sp` 保存现有内核调用链中的实际位置。

## 4. 29 条指令的读写和活跃边界

记号：`O(r)` 表示进入本次物理 `swtch` 时旧 continuation 的寄存器值；`N(r)` 表示 `new` context 中待恢复的值；`O=a0`、`N=a1` 是两个 context 基址。表中“执行后活跃边界”列出后续指令仍必须保持的入口旧值，或已经建立、必须保留到 `ret` 的新值。

### 4.1 14 条保存指令

每条 `sd` 只读源寄存器和地址基址，不修改任何 GPR，也不访问当前栈，除非 `old` 本身恰好位于栈上。

| # | 指令 | 读 | 写 | 执行后活跃边界 |
|---:|---|---|---|---|
| 1 | `sd ra, 0(a0)` | `O`、`O(ra)` | `old->ra` | `O`、`N`、`O(sp)`、`O(s0..s11)`；`O(ra)` 已提交 |
| 2 | `sd sp, 8(a0)` | `O`、`O(sp)` | `old->sp` | `O`、`N`、`O(s0..s11)`；`O(sp)` 已提交 |
| 3 | `sd s0, 16(a0)` | `O`、`O(s0)` | `old->s0` | `O`、`N`、`O(s1..s11)` |
| 4 | `sd s1, 24(a0)` | `O`、`O(s1)` | `old->s1` | `O`、`N`、`O(s2..s11)` |
| 5 | `sd s2, 32(a0)` | `O`、`O(s2)` | `old->s2` | `O`、`N`、`O(s3..s11)` |
| 6 | `sd s3, 40(a0)` | `O`、`O(s3)` | `old->s3` | `O`、`N`、`O(s4..s11)` |
| 7 | `sd s4, 48(a0)` | `O`、`O(s4)` | `old->s4` | `O`、`N`、`O(s5..s11)` |
| 8 | `sd s5, 56(a0)` | `O`、`O(s5)` | `old->s5` | `O`、`N`、`O(s6..s11)` |
| 9 | `sd s6, 64(a0)` | `O`、`O(s6)` | `old->s6` | `O`、`N`、`O(s7..s11)` |
| 10 | `sd s7, 72(a0)` | `O`、`O(s7)` | `old->s7` | `O`、`N`、`O(s8..s11)` |
| 11 | `sd s8, 80(a0)` | `O`、`O(s8)` | `old->s8` | `O`、`N`、`O(s9..s11)` |
| 12 | `sd s9, 88(a0)` | `O`、`O(s9)` | `old->s9` | `O`、`N`、`O(s10)`、`O(s11)` |
| 13 | `sd s10, 96(a0)` | `O`、`O(s10)` | `old->s10` | `O`、`N`、`O(s11)` |
| 14 | `sd s11, 104(a0)` | `O`、`O(s11)` | `old->s11` | `N`；`a0` 在此完成最后一次使用，完整旧 context 已提交 |

第 14 条之后，入口 `a0` 虽然仍保留在物理寄存器里，但已经死亡：恢复序列不再读取它。`a1` 必须保持为 `new` 基址直到第 28 条指令。

### 4.2 14 条恢复指令

| # | 指令 | 读 | 写 | 执行后活跃边界 |
|---:|---|---|---|---|
| 15 | `ld ra, 0(a1)` | `N`、`new->ra` | `ra=N(ra)` | `N`、`N(ra)`；跳转目标已装入但尚未使用 |
| 16 | `ld sp, 8(a1)` | `N`、`new->sp` | `sp=N(sp)` | `N`、`N(ra)`、`N(sp)`；旧栈已不能再通过 `sp` 到达 |
| 17 | `ld s0, 16(a1)` | `N`、`new->s0` | `s0=N(s0)` | `N`、`N(ra)`、`N(sp)`、`N(s0)` |
| 18 | `ld s1, 24(a1)` | `N`、`new->s1` | `s1=N(s1)` | 上项再加 `N(s1)` |
| 19 | `ld s2, 32(a1)` | `N`、`new->s2` | `s2=N(s2)` | 上项再加 `N(s2)` |
| 20 | `ld s3, 40(a1)` | `N`、`new->s3` | `s3=N(s3)` | 上项再加 `N(s3)` |
| 21 | `ld s4, 48(a1)` | `N`、`new->s4` | `s4=N(s4)` | 上项再加 `N(s4)` |
| 22 | `ld s5, 56(a1)` | `N`、`new->s5` | `s5=N(s5)` | 上项再加 `N(s5)` |
| 23 | `ld s6, 64(a1)` | `N`、`new->s6` | `s6=N(s6)` | 上项再加 `N(s6)` |
| 24 | `ld s7, 72(a1)` | `N`、`new->s7` | `s7=N(s7)` | 上项再加 `N(s7)` |
| 25 | `ld s8, 80(a1)` | `N`、`new->s8` | `s8=N(s8)` | 上项再加 `N(s8)` |
| 26 | `ld s9, 88(a1)` | `N`、`new->s9` | `s9=N(s9)` | 上项再加 `N(s9)` |
| 27 | `ld s10, 96(a1)` | `N`、`new->s10` | `s10=N(s10)` | 上项再加 `N(s10)` |
| 28 | `ld s11, 104(a1)` | `N`、`new->s11` | `s11=N(s11)` | `N(ra)`、`N(sp)`、`N(s0..s11)`；`a1` 在此完成最后一次使用 |

第 16 条是栈所有权的物理分界：从这一条完成起，任何隐式或显式的栈访问都会落在新栈。余下指令只以 `a1` 访问 `struct context`，不访问 `sp`，因此不需要在旧栈上完成收尾。旧栈内容没有被释放或清除；只是旧 `sp` 已安全保存在 `old->sp`，当前执行流不再通过 `sp` 引用它。

`a0/a1` 都没有恢复。逻辑上的旧 C caller 日后观察到 `swtch()` 返回时，这两个 caller-saved 寄存器可能仍含**那次恢复它的物理调用**所传的 `old/new`，而不是它先前切出时的参数；C ABI 禁止 caller 依赖它们。

### 4.3 `ret`

| # | 指令 | 读 | 写 | 执行后边界 |
|---:|---|---|---|---|
| 29 | `ret`，即 `jalr x0, 0(ra)` | `N(ra)` | `pc=N(ra)&~1`；丢弃链接值 | 从 `N(sp)` 所在栈、以 `N(s0..s11)` 继续新 continuation |

它不是返回到本次物理调用的 caller，而是跳到 `new->ra`：

- 新进程第一次运行时进入 `forkret()`；
- 恢复进程时回到该进程上次 `sched()` 内的 `swtch()` 调用之后；
- 恢复 scheduler 时回到该 hart 的 `scheduler()` 中 `swtch()` 调用之后。

一次物理 `swtch` 的 29 条指令始终在同一个 hart 上执行。所谓“进程跨 hart 恢复”发生在更长的逻辑时间线上：hart A 把进程切出并由 scheduler 释放 `p->lock`，后来 hart B 取得同一把锁并执行另一次 `swtch`，才把该进程 continuation 恢复到 B。没有一条运行中的指令在 hart 之间移动。

## 5. 锁和 continuation 的所有权转移

```text
hart h 的 stack0，scheduler 持有 p->lock
  保存 c[h].context
  恢复 p->context
p 的内核栈，仍在 hart h 且仍持有 p->lock
  ... release(p->lock) ... run ...
  ... acquire(p->lock), 修改 state, sched() ...
  保存 p->context
  恢复 c[h].context
hart h 的 stack0，scheduler 仍持有 p->lock
  c->proc = 0
  release(p->lock)
```

自旋锁的 `lk->cpu` 在一次物理切换中仍指向同一个 hart，所以锁没有被跨 CPU 偷渡。若进程以后在另一个 hart 恢复，中间已经由旧 scheduler 释放、由新 scheduler 重新取得 `p->lock`；恢复后的 C continuation 仍看到“锁已持有”，但物理所有者已经通过正常 release/acquire 改成新 hart。

sleeplock 不参与这次瞬时交接。若进程因 I/O 睡眠时仍逻辑持有某个 sleeplock，该所有权随进程 continuation 停驻；scheduler 不取得或释放它，其他进程可能继续阻塞，直到原进程恢复并显式释放。

`tp` 不在 context 中，因此恢复后仍是执行恢复动作的 hart id，`mycpu()` 会选择新的 `struct cpu`。`sched()` 在调用前把 `mycpu()->intena` 暂存到自己的 C 局部状态，返回后写入当前 hart 的 `mycpu()->intena`；这是把内核线程的“进入最外层 `push_off` 前是否开中断”语义跨 hart 搬运，而不是保存完整 `sstatus`。

## 6. CSR 前后状态矩阵

`swtch.S` 没有任何 CSR 指令。对一条**成功完成、没有同步异常**的物理 `swtch`，下表中的 CSR 在入口和第 29 条之后逐位相同；两端是同一个 hart。调度协议还保证全程 `sstatus.SIE==0`，所以不会在指令序列中接受 supervisor interrupt。

但“旧 C 调用最终返回”可能发生在另一 hart、另一次物理 `swtch` 中。`struct context` 没有保存任何 CSR，因此不能把跨时间、跨 hart 的逻辑返回误认为 CSR 快照恢复。

| CSR | 单次物理 `swtch` 的前后 | 调度路径所依赖的当前值 | 旧 continuation 日后恢复时的保证 |
|---|---|---|---|
| `sstatus` | 不读不写，所有位保持；成功路径上 `SIE` 前后均为 0 | `sched()` 显式检查 `SIE==0` | 采用恢复 hart 的 `sstatus`；只保证切换边界仍关中断。原线程的 `intena` 由 C 软件另存/恢复，不保证其余位相等 |
| `sie` | 不读不写，enable 位保持 | 是每 hart 的中断源使能配置；全局接收仍由 `sstatus.SIE` 阻断 | 采用恢复 hart 的 `sie`，不属于进程 continuation；当前启动代码虽对各 hart 做相同初始化，`swtch` 不承诺数值相等 |
| `satp` | 不读不写 | scheduler 和进程的内核 C 路径都应运行在共享 `kernel_pagetable` 上 | 采用恢复 hart 当时安装的内核 `satp`；不从 `p->context` 恢复。用户页表切换由 trampoline 完成 |
| `stvec` | 不读不写 | 会发生调度的内核 C 路径应使用 `kernelvec` | 采用恢复 hart 的 `stvec`；trap 路径负责在 `kernelvec` 与 `uservec` 之间设置它 |
| `sscratch` | 不读不写，原样保留 | 内核调度代码不依赖它；`uservec` 会先写后读 | 可能是恢复 hart 上之前留下的值，不是进程状态，也不要求与切出时相等 |
| `sepc` | 不读不写，原样保留 | `swtch` 不把它当恢复 PC；恢复 PC 在 `context.ra` | 可能已被恢复 hart 的其他 trap 改写。用户 PC 在 trapframe；`kerneltrap()` 在可能 `yield()` 前把自身 `sepc` 放入 C 局部变量，返回后再写回 |
| `scause` | 不读不写，原样保留 | 仅由 trap 处理代码解释最近原因 | 可能反映恢复 hart 的其他 trap，不随进程保存 |
| `stval` | 不读不写，原样保留 | 仅在相关异常诊断中有意义 | 可能反映恢复 hart 的其他 trap，不随进程保存 |

因此有两个不同的不变量：

1. **物理指令不变量**：在同一 hart 成功执行这 29 条指令不会改变任何 CSR。
2. **逻辑 continuation 不变量**：只恢复 C ABI 所需的 `ra/sp/s0..s11`；正确性依赖 trap 代码或当前 hart 已建立合适的 CSR 环境，而不依赖切出时 CSR 逐位复现。

## 7. 不保证项、故障模式和可审计实验

`swtch` 不负责：

- 设置 `p->state`、`c->proc`、锁状态或中断策略；
- 保存 caller-saved GPR、用户 GPR、CSR、浮点或向量状态；
- 切换页表或刷新 TLB；
- 检查 `old/new` 的对齐、映射、别名、字段偏移或 `sp/ra` 合法性；
- 为错误 context 返回错误码；错误地址通常导致 supervisor trap，继而 panic。

| 故障 | 典型表现 | 审计点 |
|---|---|---|
| `struct context` 与汇编偏移不一致 | `ra`、`sp` 或某个 `sN` 被串位恢复 | `sizeof`/`offsetof` 断言和反汇编偏移 |
| 漏存或漏载某个 `sN` | 仅优化构建、深调用或特定寄存器分配时失败 | 切换前后 `s0..s11` 哨兵 |
| 把 `tp` 当进程字段恢复 | 迁移后 `mycpu()` 指向旧 hart | 恢复点 `tp==hartid` |
| 额外持有自旋锁进入 `sched()` | 中断关闭深度和锁所有权失配 | `noff==1`、锁 trace；不要把合法 sleeplock 误报为额外自旋锁 |
| scheduler `stack0` 越界 | 污染相邻 hart 栈或内核数据，未必立即 fault | 每 hart 4 KiB 边界；它没有 guard page |
| 进程 `sp` 越过 guard | supervisor page fault/panic | `p->kstack <= sp <= p->kstack+PGSIZE` 及 fault VA |
| `new->ra` 非法或 `new->sp` 未对齐 | `ret` 后取指 fault，或首个 C prologue 失败 | context 创建点和恢复前快照 |

可重复实验应在 `swtch` 入口记录 `old/new/ra/sp/tp` 和表中八个 CSR，在 `ret` 目标最早可观测点记录同一组值与 hart id。验证应基于关系而不是固定调度顺序：

- 每次物理切换的 hart id 不变，CSR 前后相同，`tp` 不被 context 覆盖；
- `old` 的 14 个字段精确等于入口 `ra/sp/s0..s11`，`ret` 目标和新栈精确来自 `new`；
- 同一 `proc.context` 不会被两个 hart 同时加载；
- 进程恢复时 `state==RUNNING` 且 `p->lock` 由当前 hart 持有；
- scheduler 恢复后先清 `c->proc`，随后才释放 `p->lock`；
- 跨 hart 恢复只比较 continuation 字段和协议条件，不错误断言 CSR、`a0/a1` 或 `tp` 等于切出时的值。
