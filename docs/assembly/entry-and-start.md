# `entry.S` 与 `start()`：建立第一个 C 环境

本文逐指令分析 `kernel/entry.S`，并把它与 `kernel/start.c` 的 CSR 交接连接起来。完整启动链见[启动与初始化](../architecture/boot-and-init.md)，外部机器契约见[平台契约](../architecture/platform-contracts.md)。

## 1. 入口前置条件

当前构建和 QEMU 命令共同要求：

- 每个已配置 hart 以 M-mode 从物理地址 `0x80000000` 执行 `_entry`；
- `mhartid` 是小于 `NCPU` 的稠密非负编号；
- RAM 覆盖内核镜像、`.bss` 中的 `stack0` 和 `KERNBASE..PHYSTOP`；
- 地址转换尚未妨碍对这些物理地址的访问；
- 当前工具链生成的 `_entry` 不需要已初始化的用户 TLS、C 栈或内核 `gp`；
- 实现了 M/S/U 三种特权级、PMP、Sstc，以及代码所使用的 CSR；`mret` 因而把清理后的 `MPP` 置为最低实现级 U；
- 若实现 H 扩展，入口 `mstatus.MPV=0`，使 `MPP=S` 的 `mret` 进入非虚拟化 supervisor 环境而不是 VS；
- QEMU 的交接状态满足后续保留 CSR 位的假设，尤其 `mstatus.SIE=0`，从而在安装 `stvec` 前不会因已开启的 S-mode 全局中断进入未知地址。

QEMU 常规固件接口把 hart id 和 DTB 指针放在参数寄存器中，但本实现不保存该交接。`entry.S` 会重用 `a0/a1`，`kinit()` 随后还可能覆盖 RAM 中的 DTB；设备布局来自编译期常量，而不是 DTB discovery。

## 2. 栈区布局

`start.c` 定义：

```text
stack0: 4096 * NCPU bytes, 16-byte aligned

low address
  stack0 + 0*4096   hart 0 slice bottom
  stack0 + 1*4096   hart 0 initial sp / hart 1 slice bottom
  ...
  stack0 + NCPU*4096
high address
```

栈向低地址增长，所以 hart `h` 的首个 `sp` 是 `stack0 + (h+1)*4096`。各 4 KiB slice 相邻，没有 guard page、canary 或运行时边界检查。该栈不只是临时启动栈；进入 `scheduler()` 后，它继续承载该 hart 的 scheduler/idle 控制流以及没有当前进程时的内核 trap。

若 `mhartid >= NCPU`，公式直接把 `sp` 指到数组之外；故障可能表现为静默覆盖，而不一定立即 trap。

## 3. `_entry` 的真实机器指令与活跃集

源码中的 `la`、`li` 和 `call` 是伪指令。下表取自当前构建生成的 `kernel/kernel.asm`，不是对所有链接模型的承诺。`S` 表示 `stack0`，`h` 表示 `mhartid`；“live-after”只列后续指令或 C 入口仍会使用的值，不表示其他寄存器已被清零。

| PC | 当前机器指令 | 读集合 | 写集合 | live-after | 语义 |
|---|---|---|---|---|---|
| `0x80000000` | `auipc sp,0xa` | `pc` | `sp` | `sp=0x8000a000` | 形成 GOT 页基址，不是直接形成 `stack0` 地址 |
| `0x80000004` | `ld sp,328(sp)` | `sp`、内存 `0x8000a148` | `sp` | `sp=S` | 从 `_GLOBAL_OFFSET_TABLE_+0x8` 取 `stack0`；当前符号值为 `0x8000a190` |
| `0x80000008` | `lui a0,0x1` | 无 | `a0` | `sp=S,a0=4096` | `li a0,4096` 的当前展开 |
| `0x8000000a` | `csrr a1,mhartid` | CSR `mhartid` | `a1` | `sp=S,a0=4096,a1=h` | 覆盖固件可能留在 `a1` 的 DTB 指针 |
| `0x8000000e` | `addi a1,a1,1` | `a1` | `a1` | `sp=S,a0=4096,a1=h+1` | 选择 slice 顶端 |
| `0x80000010` | `mul a0,a0,a1` | `a0,a1` | `a0` | `sp=S,a0=4096(h+1)` | `a1` 自此死亡；依赖 M 扩展 |
| `0x80000014` | `add sp,sp,a0` | `sp,a0` | `sp` | `sp=S+4096(h+1)` | `a0` 自此死亡，最终 `sp` 仍为 16 字节对齐 |
| `0x80000016` | `jal 0x80000054 <start>` | `pc` | `ra,pc` | `sp`；普通返回契约下还有 `ra=0x8000001a` | 无 C 参数；控制转到 `start()` |
| `0x8000001a` | `j 0x8000001a <spin>` | `pc` | `pc` | 无 | 仅当 `start()` 走编译器普通返回路径时到达 |

当前的 GOT 取址是重要的可执行文件事实：若只读 `entry.S` 并把 `la` 当作单条 PC-relative 地址计算，会漏掉 `0x8000a148` 处的早期内存读取。重新链接、改变代码模型或工具链后，必须重新生成反汇编并更新本表。

### 3.1 `start()` 的 C prologue 和残留帧

令 hart 的 `_entry` 栈顶为 `T=S+4096(h+1)`。当前编译器在 `start` 首部生成：

| PC | 指令 | 读集合 | 写集合 | live-after |
|---|---|---|---|---|
| `0x80000054` | `addi sp,sp,-16` | `sp=T` | `sp=T-16` | `sp,ra,s0(old)` |
| `0x80000056` | `sd ra,8(sp)` | `ra,sp` | `[T-8]` | `sp,s0(old),[sp+8]=0x8000001a` |
| `0x80000058` | `sd s0,0(sp)` | `s0(old),sp` | `[T-16]` | `sp,[sp]=s0(old),[sp+8]=ra` |
| `0x8000005a` | `addi s0,sp,16` | `sp` | `s0=T` | `sp=T-16,s0=T` 和两个保存槽 |

因此 `start()` 的帧恰为 16 字节：当前 `sp` 处保存入口旧 `s0`，`sp+8` 保存 `_entry` 的链接地址。`0x800000b8` 的 `mret` 是控制转移，不会执行其后的 `ld ra,8(sp)`、`ld s0,0(sp)`、`addi sp,sp,16`、`ret`。正常启动路径进入 `main` 时 `sp=T-16`，这 16 字节一直留在每 hart 的启动/调度栈上；只有假想的普通 C 返回路径才会恢复它并跳到 `spin`。

### 3.2 C ABI 边界

| 边界 | 有效 live-in | 有效 live-out/后继契约 |
|---|---|---|
| `_entry` 调用 `start` | `sp=T` 且 16 字节对齐；`jal` 产生 `ra=0x8000001a`；函数无参数 | `start` 若按普通 C ABI 返回，应恢复 `sp,ra,s0`；设计路径不返回 |
| `start` 正文 | `sp` 和 `ra` 有效；入口 `s0` 仅因 callee-saved 规则被保存；`a0..a7` 没有参数；`tp` 尚不是 xv6 hart id | caller-saved GPR 可作临时量；当前 freestanding/medany 产物不得依赖 `_entry` 预置 `gp` |
| `mret` 到 `main` | 这不是 `call`，所以不存在普通的 C 函数返回值或 callee 返回契约 | xv6 只承诺 `pc=main`、`sp=T-16`、`tp=(int)mhartid` 及下节 CSR 状态；当前 `s0=T` 和 volatile GPR 的偶然值不是跨版本接口 |

最后一次 `jal timerinit` 会改写寄存器 `ra`；当前产物在 `mret` 前偶然留下 `ra=0x800000b0`，而 `mret` 又绕过 epilogue，所以 `main` 不能把它当作可返回到 `_entry` 的地址。链接和编译选项也必须继续保证 `start()` 及其 callees 不要求入口代码建立一个新 `gp`。

## 4. `start()` 的 CSR 变换

下表把“软件显式写 CSR”和“执行 `mret` 的体系结构副作用”分开。`old(X)` 是进入 `start()` 时的值；WARL CSR 最终只接受实现支持的编码。

| 状态/CSR 域 | `start()` 显式操作 | `mret` 前 | `mret` 隐式操作 | `main` 入口 |
|---|---|---|---|---|
| 当前特权级 | 无 | M | 取 `MPP` 作为新级别 | S |
| `mstatus.MPP` | read-modify-write 为 S | S | 置为最低实现特权级 | U（当前平台实现 U） |
| `mstatus.MIE` | 位图中原样回写 | `old(MIE)` | `MIE <- MPIE` | `old(MPIE)` |
| `mstatus.MPIE` | 位图中原样回写 | `old(MPIE)` | `MPIE <- 1` | 1 |
| `mstatus.MPRV` | 位图中原样回写 | `old(MPRV)` | 因返回目标不是 M，清零 | 0 |
| `mstatus.MPV`（若实现 H） | 位图中原样回写 | `old(MPV)` | 先与 `MPP` 一起决定目标虚拟化状态，再清零 | 入口前提为 0，故进入非虚拟化 S 且字段为 0 |
| `mstatus.SIE` | 未显式修改 | `old(SIE)` | `mret` 不修改 | 必须由平台前置条件保证为 0，直至安装 `stvec` |
| `mstatus` 其他域 | C 表达式把 MPP 之外的读回位原样送回 | 受 CSR 的 WARL/WPRI 规则约束 | 按特权规范执行扩展相关的 `mret` 副作用 | 不能由“只改 MPP”推导为全部逐位不变 |
| `mepc` / `pc` | `mepc=main` | `mepc=main` | `pc <- mepc`；`mepc` 本身不由此清除 | `pc=main` |
| `satp` | 写 0 | Bare | 不修改 | Bare，直到 `kvminithart()` |
| `medeleg` | 请求写 `0xffff` | 支持且可委托位的 WARL 子集 | 不修改 | 该子集委托给 S-mode |
| `mideleg` | 请求写 `0xffff` | 支持且可委托位的 WARL 子集 | 不修改 | 该子集委托给 S-mode |
| `sie.SEIE/STIE` | 读改写，置 1 | 两位为 1；其他 `sie` 位按读回值保留并受 WARL 约束 | 不修改 | 中断类别允许，是否响应还受 `SIE` 和 pending 控制 |
| `pmpaddr0` / `pmpcfg0` | 写 `0x3fffffffffffff` / `0xf` | entry 0 为顶界极大的 TOR RWX 区域 | 不修改 | S-mode 可访问 xv6 所需 RAM/MMIO；仍取决于实现的 PMP 粒度/WARL |
| `menvcfg.STCE` | `timerinit` 读改写置 1 | 1 | 不修改 | 允许 S-mode 使用 `stimecmp` |
| `mcounteren.TM` | `timerinit` 读改写置 1 | 1 | 不修改 | 允许 S-mode 访问 `time` |
| `stimecmp` | 写 `time+1000000` | 首个比较值 | 不修改 | 到期后形成每 hart 的 supervisor timer pending 条件 |
| `mhartid` / GPR `tp` | 只读 `mhartid`，`tp=(int)mhartid` | CSR 不变，`tp` 已设置 | 不修改 GPR | `cpuid()` 从 `tp` 取 hart id |

“其他 `mstatus` 位被保留”只可描述 `r_mstatus()`、清/置 `MPP`、`w_mstatus()` 这一段的软件意图，不能覆盖后续 `mret`。即使在这段 RMW 内，WARL/WPRI 字段也只能按平台规范解释。代码没有显式清 `SIE`，也没有规范化 `TVM/TW/TSR` 等影响 `satp`、`wfi` 或 `sret` 的域；因此“从任意 M-mode 状态均可启动”不是本实现保证，而是 QEMU/启动环境信任边界。

## 5. 栈和寄存器所有权转移

```text
QEMU owns hart execution state
  --jump _entry-->
entry owns a0/a1/sp and selects stack0[h]
  --call start-->
C ABI owns a 16-byte frame; tp not yet a valid hart id until start writes it
  --mret-->
main/scheduler owns stack0[h] below the residual frame and tp=h
```

其他 hart 不会共用同一 slice，但 `stack0` 没有锁；正确性完全来自索引唯一。`main()` 的 `started` 门闩保护的是共享初始化发布，不保护初始栈选择。

## 6. 常见故障定位

| 现象 | 首查状态 | 可能原因 |
|---|---|---|
| `_entry` 第一条附近 load fault | 最终 `la` 展开、GOT 地址 | 汇编规则或链接模型改变，GOT 未被加载/可访问 |
| 进入 `start` 后栈损坏 | `mhartid`、`sp-stack0` | hart id 越界、CPU 数与 `NCPU` 不一致、4 KiB 溢出 |
| `mret` 立即异常 | `mstatus/mepc/medeleg` | 保留 CSR 状态不满足、`main` 地址/对齐错误 |
| 首次启用页表后 fault | `satp`、PTE A/D 行为、`stvec` | 平台不自动管理 A/D 或内核映射不完整 |
| 多核只在 idle 时挂死 | `tp`、各 hart `stimecmp` | hart 身份错误或某 hart 未安排 timer |

## 7. 可重复验证

1. 反汇编 `_entry`，记录 `la` 的真实指令和是否读取 `.got`。
2. 在 `_entry+0`、`call start` 前、prologue 后、`mret` 前、`main` 首行设置断点，逐 hart 核对 `sp` 范围、16 字节对齐以及 `[sp]=old(s0)`、`[sp+8]=0x8000001a`。
3. 用 `CPUS=1` 和默认配置分别核对每个 `sp` slice 不重叠。
4. 测试性地在不提交的分支把 CPU 数设为 `NCPU+1`，应通过显式断言尽早失败；当前未经加固的版本不应作为安全实验运行在重要镜像上。
5. 检查 `mret` 前 `sstatus.SIE==0`、`mepc==main`、`tp==(int)mhartid`；单步后核对 `MPP=U`、`MPIE=1`、`MPRV=0` 和 `MIE=pre.MPIE`，并记录 WARL/扩展域形成平台升级基线。
