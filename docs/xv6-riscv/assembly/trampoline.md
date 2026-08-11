# `trampoline.S`：用户态与内核态的双页表桥

`kernel/trampoline.S` 同时映射在每个用户页表和内核页表的 `TRAMPOLINE` 虚拟地址。它解决 trap 瞬间的两个事实：CPU 已进入 S-mode，但仍使用用户页表和用户 `sp`；硬件只保存 CSR，没有替内核保存 GPR、切栈或切页表。

## 1. 固定映射与对象

```text
MAXVA
  TRAMPOLINE = MAXVA-PGSIZE      RX, same physical code page in both page tables
  TRAPFRAME  = TRAMPOLINE-PGSIZE supervisor-only in each user page table
  ... user mappings below ...
```

每个进程有独立物理 trapframe，但虚拟地址相同。用户 PTE 没有 `PTE_U`，S-mode 在用户页表下可访问；内核页表通过物理内存恒等映射访问同一页。trampoline 必须在页内，链接脚本会对尺寸做断言。

`struct trapframe` 共 288 字节：偏移 0、8、16、32 是内核入口所需的 `kernel_satp/kernel_sp/kernel_trap/kernel_hartid`，偏移 24 的 `epc` 是用户 continuation 的 PC，40..280 是 31 个非零用户 GPR。`epc` 由 C 在 `sepc` 与内存之间搬运，不由汇编的 GPR 保存表处理。物理分配仍是一整页，但页内 288 字节结构和 4096 字节分配单位不能混为一谈。

## 2. `uservec` 入口状态

硬件已完成：`sepc/scause/stval/sstatus` 更新、`SIE=0`、特权级变为 S、PC 从 `stvec` 取得。仍然成立：

- `satp` 是当前用户页表；
- `sp/tp/gp/a*` 都是用户值；
- `a0` 可能是系统调用参数或任意被中断值；
- 内核 C、进程内核栈及大部分内核地址尚不能通过当前页表访问；
- `TRAMPOLINE` 和 supervisor-only `TRAPFRAME` 可访问。

任何在切栈前使用用户 `sp` 的 push/call 都会破坏用户内存或 fault，因此保存过程完全用固定地址和寄存器完成。

## 3. 逐指令寄存器活跃集

### 3.1 为什么先用 `sscratch`

```asm
csrw sscratch, a0
li   a0, TRAPFRAME
```

需要一个寄存器作为 trapframe 基址，但所有用户寄存器都必须保留。`sscratch` 临时保存用户 `a0`，释放 `a0` 作为基址。前两条指令的精确状态是：

| 指令 | 读取 | 写入/破坏 | 指令后的必保活状态 |
|---|---|---|---|
| `csrw sscratch,a0` | 用户 `a0` | `sscratch` | 31 个用户 GPR 仍在硬件；用户 `a0` 另有 CSR 副本 |
| `li a0,TRAPFRAME` | 常量 | `a0` | 用户 `a0` 只在 `sscratch`；其余用户 GPR 仍在原寄存器；`a0` 是唯一 trapframe 基址 |

`li` 是伪指令，其机器指令展开由链接地址和压缩指令选择决定；下面的状态边界按源码指令给出，发布文档时仍应以 `kernel/kernel.asm` 核对实际展开。

### 3.2 每条 `sd/ld` 的活跃集

令保存/恢复顺序为 `Q=[ra,sp,gp,tp,t0,t1,t2,s0,s1,a1,a2,a3,a4,a5,a6,a7,s2,s3,s4,s5,s6,s7,s8,s9,s10,s11,t3,t4,t5,t6]`。在第 `i` 条保存后，`Q1..Qi` 已在内存中持久，仍必须保持硬件原值的是 `Q(i+1)..Q30`，用户 `a0` 则一直在 `sscratch`；在第 `i` 条恢复后，已经成为不可再破坏的用户硬件状态是 `Q1..Qi`，而 `a0=TRAPFRAME` 仍是后续加载基址。这个定义使下表的区间成为完整而非近似的 live-after 集：

| i | 寄存器/偏移 | `uservec` 保存指令 | 保存后的未落盘用户 GPR | `userret` 恢复指令 | 恢复后的硬件用户 GPR |
|---:|---|---|---|---|---|
| 1 | `ra/40` | `sd ra,40(a0)` | `Q2..Q30` | `ld ra,40(a0)` | `Q1` |
| 2 | `sp/48` | `sd sp,48(a0)` | `Q3..Q30` | `ld sp,48(a0)` | `Q1..Q2` |
| 3 | `gp/56` | `sd gp,56(a0)` | `Q4..Q30` | `ld gp,56(a0)` | `Q1..Q3` |
| 4 | `tp/64` | `sd tp,64(a0)` | `Q5..Q30` | `ld tp,64(a0)` | `Q1..Q4` |
| 5 | `t0/72` | `sd t0,72(a0)` | `Q6..Q30` | `ld t0,72(a0)` | `Q1..Q5` |
| 6 | `t1/80` | `sd t1,80(a0)` | `Q7..Q30` | `ld t1,80(a0)` | `Q1..Q6` |
| 7 | `t2/88` | `sd t2,88(a0)` | `Q8..Q30` | `ld t2,88(a0)` | `Q1..Q7` |
| 8 | `s0/96` | `sd s0,96(a0)` | `Q9..Q30` | `ld s0,96(a0)` | `Q1..Q8` |
| 9 | `s1/104` | `sd s1,104(a0)` | `Q10..Q30` | `ld s1,104(a0)` | `Q1..Q9` |
| 10 | `a1/120` | `sd a1,120(a0)` | `Q11..Q30` | `ld a1,120(a0)` | `Q1..Q10` |
| 11 | `a2/128` | `sd a2,128(a0)` | `Q12..Q30` | `ld a2,128(a0)` | `Q1..Q11` |
| 12 | `a3/136` | `sd a3,136(a0)` | `Q13..Q30` | `ld a3,136(a0)` | `Q1..Q12` |
| 13 | `a4/144` | `sd a4,144(a0)` | `Q14..Q30` | `ld a4,144(a0)` | `Q1..Q13` |
| 14 | `a5/152` | `sd a5,152(a0)` | `Q15..Q30` | `ld a5,152(a0)` | `Q1..Q14` |
| 15 | `a6/160` | `sd a6,160(a0)` | `Q16..Q30` | `ld a6,160(a0)` | `Q1..Q15` |
| 16 | `a7/168` | `sd a7,168(a0)` | `Q17..Q30` | `ld a7,168(a0)` | `Q1..Q16` |
| 17 | `s2/176` | `sd s2,176(a0)` | `Q18..Q30` | `ld s2,176(a0)` | `Q1..Q17` |
| 18 | `s3/184` | `sd s3,184(a0)` | `Q19..Q30` | `ld s3,184(a0)` | `Q1..Q18` |
| 19 | `s4/192` | `sd s4,192(a0)` | `Q20..Q30` | `ld s4,192(a0)` | `Q1..Q19` |
| 20 | `s5/200` | `sd s5,200(a0)` | `Q21..Q30` | `ld s5,200(a0)` | `Q1..Q20` |
| 21 | `s6/208` | `sd s6,208(a0)` | `Q22..Q30` | `ld s6,208(a0)` | `Q1..Q21` |
| 22 | `s7/216` | `sd s7,216(a0)` | `Q23..Q30` | `ld s7,216(a0)` | `Q1..Q22` |
| 23 | `s8/224` | `sd s8,224(a0)` | `Q24..Q30` | `ld s8,224(a0)` | `Q1..Q23` |
| 24 | `s9/232` | `sd s9,232(a0)` | `Q25..Q30` | `ld s9,232(a0)` | `Q1..Q24` |
| 25 | `s10/240` | `sd s10,240(a0)` | `Q26..Q30` | `ld s10,240(a0)` | `Q1..Q25` |
| 26 | `s11/248` | `sd s11,248(a0)` | `Q27..Q30` | `ld s11,248(a0)` | `Q1..Q26` |
| 27 | `t3/256` | `sd t3,256(a0)` | `Q28..Q30` | `ld t3,256(a0)` | `Q1..Q27` |
| 28 | `t4/264` | `sd t4,264(a0)` | `Q29..Q30` | `ld t4,264(a0)` | `Q1..Q28` |
| 29 | `t5/272` | `sd t5,272(a0)` | `Q30` | `ld t5,272(a0)` | `Q1..Q29` |
| 30 | `t6/280` | `sd t6,280(a0)` | 空 | `ld t6,280(a0)` | `Q1..Q30` |

恢复 `sp/gp/tp` 后这些寄存器立即成为用户值，但后续地址只通过 `a0` 形成，也没有栈访问、全局寻址或 C 调用，所以不会误用它们。全量保存是异步 trap 的要求；它比普通 C callee 只保存 `s0..s11` 的 ABI 要强。

### 3.3 保存 `a0` 后重用临时寄存器

保存表结束后：

```asm
csrr t0, sscratch
sd   t0, 112(a0)
```

`csrr` 读取 `sscratch` 并破坏已经落盘的用户 `t0`；`sd` 后 31 个用户 GPR 全部在 trapframe，`a0` 仍为基址，`t0` 不再需要保活。CSR 仍是 per-hart 状态：`usertrap()` 在允许中断前把 `sepc` 复制到 `trapframe->epc`。

`sscratch` 在返回前不恢复为固定值；它只是入口 scratch，下一次 trap 会再次覆盖。不要把其 trap 外数值当作 ABI。

## 4. 从 trapframe 建立 C 环境

| 加载 | 目标寄存器 | 生产者 | 作用 |
|---|---|---|---|
| offset 8 | `sp` | `prepare_return()` | 当前进程内核栈顶 |
| offset 32 | `tp` | `prepare_return()` | 上次返回用户态所在 hart id |
| offset 16 | `t0` | `prepare_return()` | `usertrap` 函数地址 |
| offset 0 | `t1` | `prepare_return()` | 内核 `satp` token |

这些值在每次返回用户态前刷新。进程若跨 hart 调度，`kernel_hartid` 必须在新的 hart 上更新后才 `sret`；因此下一次 trap 会恢复正确的内核 `tp`。`fork()` 只用结构体赋值复制 288 字节 `struct trapframe`，不是复制整页；子进程第一次执行 `forkret()`，随后 `prepare_return()` 会在进入用户态前修复复制来的旧 kernel 字段。

`kernel_sp=p->kstack+PGSIZE` 是向下增长的进程内核栈顶并满足 16 字节 RV64 psABI 对齐；trampoline 自身不建立栈帧。`jalr t0` 等价于写 `ra` 的间接调用，返回地址正是紧邻的 `userret`；它破坏的 `ra` 已经落盘。`usertrap(void)` 不接收参数，返回的用户 `satp` token 按 ABI 放入 `a0`。C 的 caller/callee-saved 规则从这次 `jalr` 才开始生效。

## 5. 切换到内核页表

| 指令 | 读取 | 写入/破坏 | 活跃状态/边界 |
|---|---|---|---|
| `ld sp,8(a0)` | trapframe base、`kernel_sp` | `sp` | `a0` 仍是基址；从此 C 栈可用，但当前序列尚不访问栈 |
| `ld tp,32(a0)` | base、`kernel_hartid` | `tp` | `tp` 成为本 hart id |
| `ld t0,16(a0)` | base、`kernel_trap` | `t0` | `t0=usertrap`，必须保活到 `jalr` |
| `ld t1,0(a0)` | base、`kernel_satp` | `t1` | `t0/t1` 分别是 call target/page-table token |
| `sfence.vma zero,zero` | 当前页表状态 | 本 hart 地址转换缓存/次序 | `t0/t1/sp/tp` 保活 |
| `csrw satp,t1` | `t1` | `satp` | 后续普通内核 VA 可见；PC 仍在同物理 trampoline |
| `sfence.vma zero,zero` | 新页表状态 | 本 hart地址转换缓存/次序 | `t0/sp/tp` 保活 |
| `jalr t0` | `t0` | `ra=下一条(userret)`、`pc` | 进入 `usertrap`；用户状态已全部在内存，C ABI 接管 |

两道 `sfence.vma` 保守地围住根页表切换，使相关地址转换更新与后续隐式页表访问按架构规则排序，并清除不再适用的本地转换缓存；它不是通用数据内存 fence。因为 trampoline 在两页表中同 VA 映射同一物理页，取指 PC 在切换前后连续。

`sfence.vma` 是地址转换/页表同步，不等价于 `fence.i`，也不是通用 DMA 持久化屏障。仓库没有在 `exec` 写入代码后执行完整的跨 hart instruction-cache 同步；当前行为依赖 QEMU/目标平台的额外一致性，是信任模型中的可移植性边界。

进入 `usertrap()` 时 `sp/tp/satp` 已是内核值，全部用户 GPR 已保存。`gp` 仍是保存前的用户硬件值，因为保存不改变寄存器且入口没有载入内核 `gp`；当前编译结果依赖内核不生成需要预置 `gp` 的访问。工具链参数变化必须反汇编验证。

## 6. C handler 与返回准备

`usertrap()` 立即把 `stvec` 改为 `kernelvec`，保存 `sepc`，然后按原因处理 syscall、设备中断或 lazy page fault。系统调用将 `epc += 4`；只有 load/store page fault 会尝试 `vmfault()`，成功时不推进 PC，以便重试原指令。当前 `vmfault(..., read)` 没有使用 `read` 参数，load 与 store 都物化为 `PTE_R|PTE_W|PTE_U` 的非执行页；instruction page fault 不进入该分支，最终设置 `killed`。分配失败、地址超出 `p->sz` 或页已映射也使 `vmfault()` 返回 0，并沿同一 kill 路径退出。

返回前 `prepare_return()`：

1. 关中断，防止 `stvec` 切换窗口中的可屏蔽中断误进 `uservec`；
2. 设置 `stvec` 为用户页表可见的 `TRAMPOLINE+(uservec-trampoline)`；
3. 刷新四个 kernel 字段；
4. 清 `sstatus.SPP`、置 `SPIE`；
5. 把 `trapframe->epc` 写入 `sepc`。

`usertrap()` 返回的 `a0` 是用户 `satp` token，不是用户可见返回值；真正的用户 a0 已在 trapframe offset 112。

关中断并不能屏蔽同步异常。从 `stvec=uservec` 生效起到 `sret` 为止，C epilogue、trampoline 取指、kernel/user trapframe 映射、页表切换和所有加载都必须保证不产生 page/access/illegal-instruction fault；否则内核现场会错误进入按“用户 GPR + 用户页表”设计的 `uservec`。因此不能在这段返回走廊随意插入可能 fault 的探针、栈访问或函数调用。

## 7. `userret` 控制指令与最后使用点

### 7.1 换用户页表

`usertrap` 的 `ret` 跳到 `userret`，此时 `a0=user_satp`、`sp` 仍是已退栈后的内核栈顶。逐条边界是：

| 指令 | 读取 | 写入/破坏 | 最后使用点/活跃状态 |
|---|---|---|---|
| `sfence.vma zero,zero` | 内核地址转换状态 | TLB/页表次序 | `a0=user_satp` 必须保活 |
| `csrw satp,a0` | `a0` | `satp` | `a0` token 在此最后使用；此后普通内核 VA 不可依赖 |
| `sfence.vma zero,zero` | 用户地址转换状态 | TLB/页表次序 | 只能依赖 trampoline/TRAPFRAME 映射 |
| `li a0,TRAPFRAME` | 常量 | `a0` | 建立恢复表唯一基址；旧内核栈不再访问 |
| 上表 30 条 `ld` | `a0`、trapframe | 对应 `Qi` | 每条使 `Q1..Qi` 成为不可破坏的用户状态 |
| `ld a0,112(a0)` | base、saved user `a0` | `a0` | trapframe 基址在此最后使用；31 个用户 GPR 均已恢复 |
| `sret` | `sepc/sstatus` | `pc/privilege/sstatus` | 不读取 `ra`；从 `sepc` 恢复用户 continuation |

### 7.2 以固定 VA 恢复

切到用户页表后，汇编仍在 trampoline 且不再访问旧内核栈。具体 30 条恢复指令、偏移和逐条 live-after 集见 3.2；最后恢复 `a0` 后不得再进行任何内存寻址。

### 7.3 `sret`

`sret` 使用 C 已设置的 `sepc/sstatus`：`pc<-sepc`，特权级取 `SPP`，随后 `SPP<-U`、`SIE<-SPIE`、`SPIE<-1`。当前准备态是 `SPP=U,SPIE=1`。CPU 已在 U-mode 时，S-mode 中断可响应是因为当前特权级低于 S，并不由此时观察到的 `SIE` 位额外门控。若 `SPP` 未清，会错误返回 S-mode；若 `sepc` 不是可执行用户地址，取指会再次 trap 并被杀死。

## 8. CSR 全生命周期矩阵

下表覆盖本路径读取、写入或依赖的全部 S-mode CSR；“残留”表示软件不承诺恢复入口值，尤其可能已被内核中的嵌套 trap 覆盖。

| CSR/状态 | 用户 trap 前 | 硬件入口后 | `uservec` 换内核页表后 | `prepare_return` 后 | `userret` 换用户页表后 | `sret` 后 |
|---|---|---|---|---|---|---|
| privilege/PC | U / 用户指令 | S / `stvec` | S / trampoline 再到 `usertrap` | S / C 返回走廊 | S / trampoline | U / `sepc` |
| `sstatus.SPP` | 旧残留 | 0，记录 U 来源 | 0 | 显式清 0 | 0 | 清为 0 |
| `sstatus.SIE` | 不决定 U 下 S 中断门控 | 0；旧 SIE 复制到 SPIE | 0，C 以后可临时开启 | `intr_off()` 后为 0 | 0 | 从 SPIE 置为 1 |
| `sstatus.SPIE` | 旧残留 | trap 前 SIE | 不变，除嵌套 trap | 显式置 1 | 1 | 置 1 |
| `sepc` | 旧残留 | fault/interrupt 的用户 PC | 同值，C 复制到 `trapframe->epc` | 写回可能推进/修改后的 `epc` | 不变 | CSR 仍保留，PC 从它取得 |
| `scause` | 旧残留 | 本次原因 | C 在安全点读取；之后可被嵌套 trap 覆盖 | 残留，不恢复 | 残留 | 残留 |
| `stval` | 旧残留 | 本次异常值或平台定义值 | fault 路径读取；之后可被嵌套 trap 覆盖 | 残留，不恢复 | 残留 | 残留 |
| `stvec` | `uservec` | 不变 | 进入 C 后立即改 `kernelvec` | 改回 `uservec` | `uservec` | `uservec`，下次入 C 再改 |
| `sscratch` | 无稳定 ABI | 硬件不改 | 保存入口 hart 的用户 `a0`，随后软件不再依赖 | 若处理期间迁移，则是恢复 hart 的残留 | 保留恢复 hart 的残留 | 无稳定 ABI；下次 trap 覆盖 |
| `satp` | 用户页表 | 用户页表 | 内核页表 | 内核页表 | 用户页表 | 用户页表 |
| `sie/sip` | 按设备/计时器状态 | 类别/挂起位不由入口自动清除 | handler/设备可改变 `sip` 原因 | 保持系统配置 | 保持 | 保持；投递按特权规则判断 |

`sfence.vma` 不写上述 CSR 值，只约束本 hart 地址转换观察；`scause/stval/sscratch` 没有像 `sepc` 那样的保存/恢复承诺。用户 trap 处理可以在系统调用睡眠或 timer `yield()` 时迁移，所以表中“残留”可能来自另一个 hart，不能理解为入口值物理留存到返回。

## 9. 完整 happens-before 链

```text
prepare_return fills kernel_* and sepc/sstatus with interrupts off
  -> userret switches satp and restores GPR
  -> sret publishes running user continuation
  -> hardware trap clears SIE and records sepc/cause
  -> uservec stores every user GPR
  -> page-table fences and satp switch
  -> usertrap copies sepc before enabling interrupts
```

关键不变量是：在用户 GPR 完整落盘前不调用 C；在内核入口环境建立前不访问普通内核地址；在 `stvec=uservec` 的内核窗口中既不允许可屏蔽中断，也不允许任何同步 fault。

## 10. 失败模式

| 破坏项 | 后果 |
|---|---|
| trapframe C 结构与汇编偏移漂移 | 用户寄存器交叉覆盖，通常延迟到 `sret` 后暴露 |
| TRAMPOLINE 两页表 VA/PA 不同 | `satp` 写后从错误物理页继续取指 |
| TRAPFRAME 意外带 `PTE_U` | 用户可篡改返回 PC、内核栈、页表 token，形成提权 |
| `prepare_return` 漏关中断，或返回走廊同步 fault | 内核 trap 可能走 uservec 并覆盖用户保存区 |
| stale `kernel_hartid` | `mycpu()` 指错，锁/中断计数破坏 |
| satp/fence 顺序错误 | 使用 stale TLB 或在错误页表访问栈/数据 |
| 新工具链依赖 `gp` | 进入 `usertrap` 后全局访问错误 |

## 11. 验证方法

1. 编译期断言 trapframe 每个字段偏移及总尺寸不超过一页。
2. GDB 在 `uservec` 每个阶段记录 `satp/sp/tp/a0/sscratch`，确认第一次普通内核内存访问发生在换页表后。
3. 让进程在多 hart timer 压力下迁移，核对下一次 user trap 加载的是当前 hart id。
4. 分别触发 syscall、load/store fault、UART 和 timer，比较保存的 `epc/scause` 以及 a0/a7。
5. 把用户寄存器填为各不相同的模式，trap 往返后逐项比较；这能发现偏移错位但不能替代页表权限检查。
6. 检查用户页表中 TRAPFRAME 不含 `PTE_U`、TRAMPOLINE 只读可执行，且物理页与内核映射一致。
