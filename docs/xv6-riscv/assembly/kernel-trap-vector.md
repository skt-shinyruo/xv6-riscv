# `kernelvec.S`：内核态 trap 的保存边界

当 S-mode 内核执行期间发生 timer、外部中断或异常，处理器依据 `stvec` 跳到 `kernelvec`。这条路径不切页表、不换栈，也不使用进程的用户 trapframe；它在当前内核栈上保存一组精确限定的 GPR，调用 `kerneltrap()`，再恢复现场并执行 `sret`。

本文逐条对应 [`kernel/kernelvec.S`](../../../kernel/kernelvec.S)、[`kernel/trap.c`](../../../kernel/trap.c) 和 [`kernel/proc.h`](../../../kernel/proc.h)。这里的“活跃”指：为了让被打断的内核 continuation 正确继续，某个入口值仍必须能从寄存器、当前保存帧或更深的 C 栈帧中恢复。

## 1. 入口契约与硬件动作

设被打断的内核 PC 为 `K`，trap 前 `sstatus.SIE=b`，其中 `b` 为 0 或 1。入口必须满足：

- 当前特权级为 S-mode，`stvec.BASE=kernelvec` 且 `stvec.MODE=Direct`；`trapinithart()` 和 `usertrap()` 建立这一条件；
- `satp` 选择共享的内核页表；当前 `sp` 是进程内核栈，或无当前进程时本 hart 的 `stack0`；
- `tp` 是当前 hart id；xv6 用它索引 `cpus[]`，而不是把它当普通线程局部指针；
- 当前实现没有独立的中断栈、异常恢复表或 NMI 栈，内核同步异常通常最终 panic。

硬件接收 trap 时原子地完成控制状态转移：

```text
sepc         <- K
scause       <- trap cause
stval        <- 0 for an interrupt, or exception-specific detail when defined
sstatus.SPIE <- b
sstatus.SIE  <- 0
sstatus.SPP  <- 1
privilege    <- S
pc           <- stvec.BASE
```

硬件不会保存任何 GPR，不会修改 `sp/tp/satp/stvec`。因此第一条汇编指令仍在被打断代码的内核栈上执行。`prepare_return()` 把 `stvec` 改成 `uservec` 前先关中断，并在 `sret` 前一直保持关闭；否则这条入口契约会被破坏。

## 2. 256 字节保存帧：所有槽位

`addi sp,sp,-256` 分配 32 个 8 字节槽，保持 RISC-V C ABI 要求的 16 字节栈对齐。下表覆盖帧内每个槽；“空洞”表示 `kernelvec` 从不写也不读该地址，而不是它含有一个隐式保存值。

| 偏移 | 常规寄存器位置 | `kernelvec` 行为 | 原因 |
|---:|---|---|---|
| 0 | `ra` | `sd/ld` | caller-saved，且 `call` 会直接改写 |
| 8 | `sp` | 空洞 | 入口 `sp` 恒等于帧基址 `+256` |
| 16 | `gp` | `sd/ld` | ABI fixed register；防止工具链或未来 C 路径改变 |
| 24 | `tp` | 空洞 | 必须保留恢复 hart 的 id，不得恢复入口 hart id |
| 32 | `t0` | `sd/ld` | caller-saved |
| 40 | `t1` | `sd/ld` | caller-saved |
| 48 | `t2` | `sd/ld` | caller-saved |
| 56 | `s0/fp` | 空洞 | callee-saved，由 C ABI 调用链负责 |
| 64 | `s1` | 空洞 | callee-saved，由 C ABI 调用链负责 |
| 72 | `a0` | `sd/ld` | caller-saved |
| 80 | `a1` | `sd/ld` | caller-saved |
| 88 | `a2` | `sd/ld` | caller-saved |
| 96 | `a3` | `sd/ld` | caller-saved |
| 104 | `a4` | `sd/ld` | caller-saved |
| 112 | `a5` | `sd/ld` | caller-saved |
| 120 | `a6` | `sd/ld` | caller-saved |
| 128 | `a7` | `sd/ld` | caller-saved |
| 136 | `s2` | 空洞 | callee-saved，由 C ABI 调用链负责 |
| 144 | `s3` | 空洞 | callee-saved，由 C ABI 调用链负责 |
| 152 | `s4` | 空洞 | callee-saved，由 C ABI 调用链负责 |
| 160 | `s5` | 空洞 | callee-saved，由 C ABI 调用链负责 |
| 168 | `s6` | 空洞 | callee-saved，由 C ABI 调用链负责 |
| 176 | `s7` | 空洞 | callee-saved，由 C ABI 调用链负责 |
| 184 | `s8` | 空洞 | callee-saved，由 C ABI 调用链负责 |
| 192 | `s9` | 空洞 | callee-saved，由 C ABI 调用链负责 |
| 200 | `s10` | 空洞 | callee-saved，由 C ABI 调用链负责 |
| 208 | `s11` | 空洞 | callee-saved，由 C ABI 调用链负责 |
| 216 | `t3` | `sd/ld` | caller-saved |
| 224 | `t4` | `sd/ld` | caller-saved |
| 232 | `t5` | `sd/ld` | caller-saved |
| 240 | `t6` | `sd/ld` | caller-saved |
| 248 | 无 | 尾部空洞 | 只用于把帧补到 256 字节 |

因此，保存集合不是“所有调用者可见 GPR”，而是精确的

```text
Q = [ra, gp, t0, t1, t2,
     a0, a1, a2, a3, a4, a5, a6, a7,
     t3, t4, t5, t6]
```

其中 `ra/t0..t6/a0..a7` 是标准 caller-saved 集，`gp` 是额外保存的 fixed register。`sp` 用算术恢复，`s0..s11` 依赖 C ABI，`tp` 则有意不透明恢复。帧也完全不保存浮点、向量或其他扩展状态。

## 3. 每条指令的读、写与活跃集

为了让逐指令表既精确又可核查，定义：

- `O(r)`：trap 入口时寄存器 `r` 的值；
- `M[r]`：当前 256 字节帧的对应槽已持久保存 `O(r)`；
- `G[r]`：恢复阶段寄存器 `r` 已重新持有 `O(r)`；
- `S_i=Q[0:i]`、`U_i=Q[i:17]`：按上面 `Q` 的顺序，前 `i` 个已保存或恢复，后面的仍待处理；
- `A={s0..s11}`：这些入口值始终在 ABI 意义上活跃；若 C 代码使用它们，值会暂存在更深的 C 栈帧，返回时重新落回寄存器；
- `H`：`tp` 中的当前 hart id。迁移后 `H` 可以不同于入口值。

### 3.1 建帧与 17 条保存指令

每条 `sd r,off(sp)` 读 `sp` 和 `O(r)`、写一个私有内核栈槽，不改 GPR。表中的活跃集只省略在所有行都成立的 `A` 和 `H`。

| # | 指令 | 读 | 写 | 指令后所需入口值的位置 |
|---:|---|---|---|---|
| 0 | `addi sp,sp,-256` | `O(sp)` | `sp=O(sp)-256` | `O(Q)` 仍在寄存器；`O(sp)=sp+256` |
| 1 | `sd ra,0(sp)` | `sp,O(ra)` | `M[ra]` | `M[S_1] + O[U_1]` |
| 2 | `sd gp,16(sp)` | `sp,O(gp)` | `M[gp]` | `M[S_2] + O[U_2]` |
| 3 | `sd t0,32(sp)` | `sp,O(t0)` | `M[t0]` | `M[S_3] + O[U_3]` |
| 4 | `sd t1,40(sp)` | `sp,O(t1)` | `M[t1]` | `M[S_4] + O[U_4]` |
| 5 | `sd t2,48(sp)` | `sp,O(t2)` | `M[t2]` | `M[S_5] + O[U_5]` |
| 6 | `sd a0,72(sp)` | `sp,O(a0)` | `M[a0]` | `M[S_6] + O[U_6]` |
| 7 | `sd a1,80(sp)` | `sp,O(a1)` | `M[a1]` | `M[S_7] + O[U_7]` |
| 8 | `sd a2,88(sp)` | `sp,O(a2)` | `M[a2]` | `M[S_8] + O[U_8]` |
| 9 | `sd a3,96(sp)` | `sp,O(a3)` | `M[a3]` | `M[S_9] + O[U_9]` |
| 10 | `sd a4,104(sp)` | `sp,O(a4)` | `M[a4]` | `M[S_10] + O[U_10]` |
| 11 | `sd a5,112(sp)` | `sp,O(a5)` | `M[a5]` | `M[S_11] + O[U_11]` |
| 12 | `sd a6,120(sp)` | `sp,O(a6)` | `M[a6]` | `M[S_12] + O[U_12]` |
| 13 | `sd a7,128(sp)` | `sp,O(a7)` | `M[a7]` | `M[S_13] + O[U_13]` |
| 14 | `sd t3,216(sp)` | `sp,O(t3)` | `M[t3]` | `M[S_14] + O[U_14]` |
| 15 | `sd t4,224(sp)` | `sp,O(t4)` | `M[t4]` | `M[S_15] + O[U_15]` |
| 16 | `sd t5,232(sp)` | `sp,O(t5)` | `M[t5]` | `M[S_16] + O[U_16]` |
| 17 | `sd t6,240(sp)` | `sp,O(t6)` | `M[t6]` | `M[S_17]=M[Q]` |

保存期间 `sp` 是帧的唯一寻址基址，因此任何插入代码都不能在未另存基址时改写它。虽然早期保存的寄存器原值已在内存中，源码在保存完成前也没有使用这些寄存器作为 scratch；这使逐行偏移核对保持简单。

### 3.2 `call kerneltrap`

| 指令 | 直接读 | 直接写 | ABI/控制效果 | 返回后的活跃状态 |
|---|---|---|---|---|
| `call kerneltrap` | PC；链接器可能把伪指令展开为 `auipc/jalr` 或放松为 `jal` | `ra` 和 PC | C 可覆盖 `ra,t0..t6,a0..a7`；必须保持 `gp`、恢复 `s0..s11`，并把 `sp` 带回当前帧基址 | `M[Q] + A + H`；`sp` 仍是帧基址 |

`kerneltrap()` 进入后立即把 `sepc/sstatus/scause` 复制到自己的 C 局部变量，验证 `SPP=1` 和 `SIE=0`，再调用 `devintr()`。若 timer trap 有当前进程，它可以执行：

```text
kernelvec frame stays on the process kernel stack
  -> kerneltrap -> yield -> sched -> swtch
  ... scheduler may resume this process on another hart ...
  -> swtch -> sched -> yield -> kerneltrap
  -> restore sepc and sstatus -> return to kernelvec
```

`swtch` 保存的 `struct context` 包含 `ra/sp/s0..s11`，因此整个 C 调用链和上面的 256 字节帧留在进程内核栈上；它们不会复制到 scheduler 栈。`tp` 不在 `struct context` 中，所以恢复后的 `H` 自然是新 hart id。无当前进程时使用 `stack0`，timer 分支不会 `yield()`。

### 3.3 17 条恢复指令、退帧与 `sret`

恢复顺序与保存顺序相同。对第 `i` 条 `ld`，`G[S_i]` 已在寄存器中且后续汇编不得改写，`M[U_i]` 仍是尚待恢复的必要副本；已恢复槽中的旧副本仍物理存在，但不再属于必要活跃集。所有行仍隐含 `A`、`H` 和帧基址 `sp` 活跃。

| # | 指令 | 读 | 写 | 指令后所需入口值的位置 |
|---:|---|---|---|---|
| 1 | `ld ra,0(sp)` | `sp,M[ra]` | `G[ra]` | `G[S_1] + M[U_1]` |
| 2 | `ld gp,16(sp)` | `sp,M[gp]` | `G[gp]` | `G[S_2] + M[U_2]` |
| 3 | `ld t0,32(sp)` | `sp,M[t0]` | `G[t0]` | `G[S_3] + M[U_3]` |
| 4 | `ld t1,40(sp)` | `sp,M[t1]` | `G[t1]` | `G[S_4] + M[U_4]` |
| 5 | `ld t2,48(sp)` | `sp,M[t2]` | `G[t2]` | `G[S_5] + M[U_5]` |
| 6 | `ld a0,72(sp)` | `sp,M[a0]` | `G[a0]` | `G[S_6] + M[U_6]` |
| 7 | `ld a1,80(sp)` | `sp,M[a1]` | `G[a1]` | `G[S_7] + M[U_7]` |
| 8 | `ld a2,88(sp)` | `sp,M[a2]` | `G[a2]` | `G[S_8] + M[U_8]` |
| 9 | `ld a3,96(sp)` | `sp,M[a3]` | `G[a3]` | `G[S_9] + M[U_9]` |
| 10 | `ld a4,104(sp)` | `sp,M[a4]` | `G[a4]` | `G[S_10] + M[U_10]` |
| 11 | `ld a5,112(sp)` | `sp,M[a5]` | `G[a5]` | `G[S_11] + M[U_11]` |
| 12 | `ld a6,120(sp)` | `sp,M[a6]` | `G[a6]` | `G[S_12] + M[U_12]` |
| 13 | `ld a7,128(sp)` | `sp,M[a7]` | `G[a7]` | `G[S_13] + M[U_13]` |
| 14 | `ld t3,216(sp)` | `sp,M[t3]` | `G[t3]` | `G[S_14] + M[U_14]` |
| 15 | `ld t4,224(sp)` | `sp,M[t4]` | `G[t4]` | `G[S_15] + M[U_15]` |
| 16 | `ld t5,232(sp)` | `sp,M[t5]` | `G[t5]` | `G[S_16] + M[U_16]` |
| 17 | `ld t6,240(sp)` | `sp,M[t6]` | `G[t6]` | `G[S_17]=G[Q]` |
| 18 | `addi sp,sp,256` | 帧基址 | `sp=O(sp)` | `G[Q] + A + O(sp) + H`；帧已失效 |
| 19 | `sret` | `sepc,sstatus` | PC、特权级、`sstatus.SIE/SPIE/SPP` | 被打断 continuation 的 GPR 状态；`tp=H` |

每条 `ld` 只写一个目标寄存器，`G[S_i]` 是该指令与此前各条 `ld` 的累积结果。`sret` 不使用 `ra`，返回 PC 来自 `sepc`；恢复后的 `ra` 只是被打断代码随后可见的原值。

## 4. Trap 相关 CSR 的完整状态矩阵

下表覆盖这条路径实际依赖的 S-mode trap CSR。记 `V=kernelvec` 的 Direct-mode `stvec` 值，`P=MAKE_SATP(kernel_pagetable)`，`C0/T0` 为本次 trap 写入的 `scause/stval`，`C*/T*` 为经历调度后恢复 hart 上最后留下的值，`X` 为 trap 前无关的旧值。

| 状态 | trap 前 | 硬件入口后 | `kerneltrap()` 返回前 | `sret` 后 |
|---|---|---|---|---|
| PC | `K` | `V.BASE` | `kernelvec` 的恢复段 | `K` |
| 特权级 | S | S | S | S，因为入口快照中 `SPP=1` |
| `sepc` | `X` | `K` | 显式 `w_sepc(sepc)`，恢复为 `K` | 仍为 `K`；`sret` 读取但不清除它 |
| `scause` | `X` | `C0` | `C*`，不恢复 | 仍为 `C*`，`sret` 不读取它 |
| `stval` | `X` | `T0` | `T*`，不恢复 | 仍为 `T*`，`sret` 不读取它 |
| `sstatus.SIE` | `b` | 0 | 显式恢复入口后的快照，仍为 0 | `b`，由 `SPIE` 复制 |
| `sstatus.SPIE` | 旧值 | `b` | 显式恢复为 `b` | 1 |
| `sstatus.SPP` | 旧值 | 1 | 显式恢复为 1 | 0 |
| `sstatus` 其他可写位 | 入口值 | 本实现依赖的基本位之外保持入口值 | `w_sstatus(sstatus)` 写回读取的整字；WARL/只读位仍按硬件规则 | 保持快照；扩展定义的 `xRET` 副作用除外 |
| `stvec` | `V` | `V` | 期望仍为 `V`，但本函数不保存/恢复 | `V` |
| `satp` | `P` | `P` | 当前 continuation 恢复时必须是 `P`；本向量不写它 | `P` |
| `sscratch` | `X` | `X` | 不使用、不恢复 | 不变 |
| `sie` | 入口值 | 不变 | 本路径不写；设备屏蔽策略另行维护 | 不变 |
| `sip`/设备 pending | pending 集 | 包含触发原因 | handler 可能通过 `stimecmp`、设备 ACK 或 PLIC claim/complete 改变 | handler 后状态 |

`kerneltrap()` 保存的是硬件入口**之后**的整个 `sstatus` 快照，所以返回汇编时必须仍满足 `SIE=0, SPIE=b, SPP=1`。它显式恢复 `sepc/sstatus`，原因是 `yield()` 期间此 continuation 可以离开当前 hart，而其他 trap 会改写各 hart 的 CSR。它不恢复 `scause/stval`：C 代码在调度前已把需要的 `scause` 存入局部变量，`sret` 也不消费这两个 CSR。

`stvec/satp` 不是保存帧的一部分。一个挂起的进程等待期间，原 hart 可以短暂运行用户页表；但恢复 `kerneltrap` 的 hart 必须已经处于共享内核页表，且内核可接收中断时 `stvec` 必须再次是 `kernelvec`。这是用户/内核 trap 路径共同维护的全局契约，不是本汇编的恢复动作。

### 4.1 `sret` 的精确变换

对本路径，执行 `sret` 的有效变换是：

```text
pc              <- sepc (= K)
privilege       <- (SPP == 1 ? S : U) = S
sstatus.SIE     <- sstatus.SPIE (= b)
sstatus.SPIE    <- 1
sstatus.SPP     <- 0
```

所以 `sret` 恢复 trap 前的中断使能位。当前能正常返回的 `devintr()` 分支都是 S-mode 可接收的设备中断，通常有 `b=1`；若 `b=0` 时发生同步异常，当前实现会因 `devintr()==0` panic。只有未来增加可恢复异常或测试 hook 后，`b=0` 的返回语义才实际可见。清零 `SPP` 是为下一次 `sret` 做准备，不意味着这一次返回到 U-mode。

## 5. C ABI、内核栈和跨 hart 恢复

```text
higher address
O(sp)       +---------------------------+
            | interrupted kernel frames |
O(sp)-256   +---------------------------+ <- kernelvec frame / call-time sp
            | kerneltrap C frame        |
            | devintr / driver frames   |
            | yield / sched frames      |
lower       +---------------------------+
```

关键边界如下：

- `kernelvec` 是硬件入口，不是一次正常 C 调用；编译器没有机会在 trap 前 spill caller-saved 活跃值，因此汇编必须先保存 `Q`；
- `kerneltrap()` 是一次正常 C 调用，所有会被它使用的 `s0..s11` 由各层函数序言/尾声保存恢复；`kernelvec` 不需要重复保存；
- `yield()->sched()->swtch()` 保存当前 C continuation 的 `ra/sp/s0..s11`。进程稍后恢复时，C 栈和 kernelvec 帧原地继续；
- `tp` 属于 hart，而不是进程。`swtch` 和 `kernelvec` 都不恢复旧 `tp`，因此跨 hart 恢复后 `cpuid()` 得到新 hart；
- `gp` 按 ABI 不应由普通 C 代码改写，但帧仍保存它，使被打断 continuation 不依赖这一工具链假设的细节；
- 256 字节之外还要容纳 handler、driver、`wakeup()` 和调度调用链。进程内核栈与 `stack0` 都只有一页，且没有运行时 high-water 保护。

这里的 `s0..s11` 保护只针对遵守 ABI 的汇编/C 调用链。新增手写汇编若破坏 callee-saved 规则，kernelvec 的空洞不会替它兜底。浮点或向量一旦在内核启用，也必须另行定义 eager/lazy 保存协议。

## 6. 控制顺序、嵌套与设备完成

这条路径的控制依赖是：

```text
hardware records cause and clears SIE
  -> kernelvec saves exactly Q
  -> kerneltrap copies sepc/sstatus/scause to C locals
  -> devintr acknowledges the source and may wake waiters
  -> kerneltrap restores entry sepc+sstatus
  -> kernelvec restores Q and sp
  -> sret resumes K with the prior interrupt-enable state
```

它不是一个跨 hart 的通用 memory happens-before 证明。设备完成与等待者之间的可见性还依赖 driver lock、进程锁、PLIC/device MMIO 规则以及 `sleep/wakeup` 协议；私有内核栈上的 `sd/ld` 本身只恢复本 continuation。

入口时硬件已关闭中断，当前 `kerneltrap()` 也不主动 `intr_on()`。若未来允许 handler 内嵌套中断，必须先为嵌套层保存 `sepc/scause/stval/sstatus`，证明栈深度和 handler 可重入性，并维持正确的 `stvec/satp`；只设置 `SIE` 不足以保证正确。

同步异常若 `devintr()==0` 会 panic。内核没有像用户 lazy fault 那样的恢复表，无效内核指针、非法指令和未识别 trap 都属于 fatal failure。

| 来源 | `devintr` 行为 | 清除/完成点 | 可能唤醒 |
|---|---|---|---|
| timer | `clockintr()`；hart 0 增加 `ticks` | 写下一次 `stimecmp` deadline | hart 0 唤醒 `&ticks`；随后当前进程可 yield |
| UART | PLIC claim 一个 source，driver 排空当前可处理事件 | 读写设备状态后 PLIC complete | console reader、UART writer |
| VirtIO | PLIC claim，driver 消费 used entries | ACK device，再 PLIC complete | 等待对应 buffer 的进程 |
| 未知 PLIC source | 打印后仍 complete | 若 level 原因未清，会再次 pending | 无，可能形成中断风暴 |

一次 external trap 只做一次 PLIC claim；设备 handler 内部可以批量处理多个事件。“一次 trap”不能等同于“一个字节”或“一个磁盘请求”。

## 7. 维护断言与验证实验

修改本路径时至少保持这些断言：

1. 帧大小是 16 的倍数，所有 `sd/ld` 偏移成对且落在 `[0,255]`；偏移 8、24、56、64、136..208、248 必须保持未使用，除非同步更新保存协议。
2. `call` 前 `Q` 已全部保存；`call` 后恢复完成前不能把任何恢复出的 `G[r]` 当 scratch。
3. `kerneltrap()` 所有正常返回路径都在最后恢复 `sepc/sstatus`；`scause/stval` 不得在 yield 后重新当作本次 trap 的可靠来源。
4. `tp` 保持恢复 hart id，`sp` 精确加回 256，`sret` 前 `satp` 是内核页表且 `stvec` 是 `kernelvec`。
5. 新增内核浮点/向量或不遵守 ABI 的汇编时，必须扩展这里的保存边界。

可执行的验证实验：

1. 让进程进入足够长的内核路径，用测试 hook 在 `stvec=kernelvec` 后把本 hart timer deadline 设到近未来；断在入口，核对 `sp` 位于该进程内核栈且 `sepc` 位于 kernel text。用户态忙循环只验证 `uservec`，不验证本向量。
2. 在保存完成后检查 17 个槽等于入口模式，并确认偏移 8、24、56、64、136..208、248 未被写；`sret` 后逐一核对 `Q` 和 `s0..s11`。
3. 在 timer yield 前后记录 hart id，强制寻找跨 hart 恢复；确认 `tp` 变为恢复 hart，而 `sp/Q/s0..s11/sepc` 仍对应原 continuation。
4. 从 `SIE=1` 的内核点触发可正常返回的设备中断，验证硬件入口为 `SIE=0` 且 `sret` 后恢复为 1；若要验证 `b=0` 的返回语义，先加入只供测试的可恢复同步 trap hook，否则当前内核应 panic。两种返回都检查 `SPIE=1,SPP=0`。
5. 在 yield 期间制造另一个 trap 覆盖 CSR，验证返回前 `sepc/sstatus` 被恢复而 `scause/stval` 可以保留新值，且 continuation 仍从 `K` 正确执行。
6. 同时制造 UART 与 VirtIO I/O，检查每次 PLIC claim/complete 配对，剩余 source 由后续 trap 处理。
7. 记录内核栈最低 `sp`，评估加入调试打印或 fault hook 后的余量；不要用共享 `fs.img` 做破坏性栈越界实验。
