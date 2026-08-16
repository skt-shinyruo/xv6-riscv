# 一次系统调用如何往返

## 问题场景与本单元成果

用户程序调用 `getpid()` 时，用户态没有普通 C 函数能直接读取内核中的
`struct proc`。生成的 stub 必须执行 `ecall`，让硬件进入 supervisor mode，
再由 trampoline 保存现场、切换页表与栈，交给内核分派，最后沿原路恢复。

本单元的唯一出口产物是一份可独立审查的 `getpid` 往返报告包。报告中的
正常与未知编号两个分节必须记录同一组 checkpoint，包括当前特权级、`satp`、
用户/内核栈、`sepc/scause/sstatus/stvec`、关键通用寄存器、trapframe 字段、
分派编号、handler 命中次数、返回值和返回 PC；只写调用链摘要不能通过。

## 前置单元与暂存黑盒

硬前置：[用户程序如何成为可运行镜像](user-program-and-abi.md)。相关基础：
[用 GDB 观察寄存器、内存和栈](../foundation/guided-debugging.md)。

本单元解除 `syscall-kernel-entry`，解释一次普通系统调用怎样跨过用户/内核
边界并返回。它只观察 trampoline 在两套页表中的同址执行和 `satp` 切换结果，
不解释映射怎样建立、PTE 权限、TLB shootdown、多 hart 迁移或返回路径的完整
安全证明；这些仍属于 `full-trampoline-page-table-contract`，由后续进程与
内存单元解除。

`getpid()` 没有用户指针、锁、睡眠和持久化状态。参数复制、阻塞 handler、
`fork/exec/exit` 的特殊终局也不在本单元推广；这里只建立普通数值返回的最小
往返模型。

## 最小模型和关键不变量

### 三份状态各有职责

一次往返同时使用三类状态，不能把它们都叫“保存寄存器”：

| 状态载体 | 本次保存什么 | 不保存什么 |
|---|---|---|
| 硬件 CSR | trap 原因 `scause`、原用户 PC `sepc`、前一特权级和中断状态 `sstatus`、入口 `stvec` | 全部用户通用寄存器、用户页表和用户栈 |
| `struct trapframe` | 用户通用寄存器、保存的用户 PC，以及下次进入内核所需的 `kernel_satp/kernel_sp/kernel_trap/kernel_hartid` | 可睡眠的内核 C 调用栈 |
| 进程内核栈 | `usertrap -> syscall -> sys_getpid` 的 C 调用链与局部状态 | 下一次返回用户态所需的完整用户寄存器集 |

`sstatus.SPP` 表示 trap 前的特权级，不是“当前正在运行的特权级”。在
`uservec` 和 `userret` 中 CPU 都仍处于 supervisor mode；`SPP=0` 只说明
`sret` 的目标是 user mode。

### 正常路径的状态表

令 `E` 为本次 `ecall` 的地址。当前 `_usertests:getpid` 的 `li` 可以是 2
字节压缩指令，但 `ecall` 本身是 4 字节，因此不变量是 `E + 4`，不是“stub
起点加 4”。实际数值地址只属于本次构建记录。

| checkpoint | 当前模式 | 当前页表/栈 | 关键状态 | 下一跳 |
|---|---|---|---|---|
| `getpid` stub | U | user `satp` / user `sp` | `li a7, SYS_getpid` | `ecall` |
| runtime `uservec` | S | user / user | `sepc=E`、`scause=8`、`SPP=0`；硬件没有改 `satp/sp` | 保存现场 |
| `uservec` 已保存 | S | user / user | user `a0/a7/sp` 已在固定 `TRAPFRAME` 偏移 `112/168/48` | 装入 `kernel_sp` |
| `uservec` 已换栈 | S | user / kernel | `sp=trapframe->kernel_sp`，随后恢复 kernel hart id | 装入 kernel `satp` |
| `usertrap` | S | kernel / kernel | 保存 `r_sepc()`；确认 `scause==8` 后令 `trapframe->epc=E+4` | `syscall()` |
| `syscall` | S | kernel / kernel | 从 `trapframe->a7` 读编号 `11` | `syscalls[11]` |
| `sys_getpid` 返回 | S | kernel / kernel | handler 返回当前 `p->pid`，分派器写入 `trapframe->a0` | `prepare_return` |
| `prepare_return` | S | kernel / kernel | 关中断，设置 runtime `uservec`、`SPP=0`、`SPIE=1`、`sepc=E+4` | runtime `userret` |
| `userret` 入口 | S | kernel / kernel | `a0` 暂存 encoded user `satp`，`sp` 仍是 kernel stack | 切 user `satp` |
| `userret` 恢复后 | S | user / user | 从 trapframe 恢复 `sp/a7/.../a0`，结果位于 `a0` | `sret` |
| stub `ret` 前 | U | user / user | `pc=E+4`、`a0=pid`、`a7=11` | `ret` 回 C caller |

`uservec` 必须先用 `sscratch` 暂存用户 `a0`，再借 `a0=TRAPFRAME` 保存其他
寄存器；若先覆盖且不暂存，原返回值/第 0 个参数就不可恢复。它在用户页表下
读取四个 `kernel_*` 字段，换成内核栈并安装 kernel `satp` 后，才通过
`jalr` 进入 C。

`usertrap()` 在读取原始 `sepc/scause/sstatus` 并推进 `epc` 后才 `intr_on()`。
此后若内核中断发生，硬件 CSR 可以被覆盖，所以在 `syscall()` 断点才读取
“原始 `scause`”不是可信证据；持久化后的用户 PC 应从 trapframe 读取。

### 未知编号仍必须完成一次返回

当前表的最大有效编号是 `21`，数组长度为 `22`。实验在 stub 已执行 `li`、
即将执行 `ecall` 时只用 GDB 把 `a7` 改成 `22`：

```text
a7=22 -> uservec -> usertrap(epc=E+4) -> syscall guard
      -> 不调用 sys_getpid -> trapframe->a0=UINT64_MAX
      -> prepare_return -> userret -> sret -> pc=E+4, a0=-1
```

必须同时看到一次精确内核诊断片段 `<pid> usertests: unknown sys call 22`、
`sys_getpid` 命中 `0` 次和只到达一次 `E+4`。若失败分派没有推进 `epc`，返回
后会再次执行同一条 `ecall`；因此“未知编号安全返回”和 `epc += 4` 是同一个
边界 oracle 的两面。`usertests` 自己先打印的 `test reparent: ` 可能与诊断
拼在同一控制台行；判定精确片段和次数，不虚构换行边界。

关键不变量如下：

- 硬件 trap 只改变特权/CSR/PC；`uservec` 才保存通用寄存器并切换页表与栈。
- 切 kernel `satp` 前必须取得 `kernel_sp/kernel_hartid/kernel_trap/kernel_satp`；
  切 user `satp` 后只能继续执行两边同址可见的 trampoline。
- `trapframe->a7` 必须保持 stub 写入的编号；handler 结果最终写回并由
  `userret` 恢复同一个 `trapframe->a0`。
- 对系统调用，`trapframe->epc` 恰好从 `E` 变为 `E+4`；未知编号也不例外。
- `prepare_return` 必须在写 `stvec` 与返回 CSR 的过渡期关闭中断，且在
  `sret` 前满足 `SPP=0`、`sepc=E+4`。

## 源码追踪计划

先用稳定 token 建立静态链，不把本次构建地址写成长期契约：

```sh
rg -n 'entry\("getpid"\)' user/usys.pl
rg -n '^#define SYS_getpid ' kernel/syscall.h
rg -n '^struct trapframe|kernel_satp|kernel_sp|uint64 epc|uint64 a0|uint64 a7' kernel/proc.h
rg -n '^uservec:|sscratch|ld sp,|csrw satp|jalr|^userret:|sret' kernel/trampoline.S
rg -n '^usertrap\(|epc \+= 4|intr_on\(|^prepare_return\(|w_stvec|w_sepc' kernel/trap.c
rg -n '^static uint64 \(\*syscalls|SYS_getpid|^syscall\(|unknown sys call' kernel/syscall.c
rg -n '^sys_getpid\(' kernel/sysproc.c
rg -n 'TRAMPOLINE|TRAPFRAME' kernel/memlayout.h
rg -n 'MAXVA|SSTATUS_SPP|SSTATUS_SPIE|MAKE_SATP' kernel/riscv.h
```

把调用边与数据边分开画出：

```text
control: getpid stub -> uservec -> usertrap -> syscall -> sys_getpid
         -> prepare_return -> userret -> sret -> stub ret

number:  SYS_getpid -> a7 -> trapframe.a7 -> syscalls[] index
result:  p.pid -> handler return -> trapframe.a0 -> restored a0
pc:      ecall E -> sepc E -> trapframe.epc E+4 -> sepc E+4 -> sret
```

`user/usys.S`、`user/usertests.asm`、`user/usertests.sym` 和
`kernel/kernel.asm` 是构建产物。用它们检查当前 ELF 与指令地址，但其来源
仍分别是生成器、用户源码、汇编源码和链接过程。

## 观察任务

资源目录提供一个标准库 runner 和它实际交给 GDB 的可读模板：

- [`run-trace.py`](../resources/syscall-roundtrip/run-trace.py)
- [`trace.gdb.in`](../resources/syscall-roundtrip/trace.gdb.in)
- [`getpid-trace-worksheet.md`](../resources/syscall-roundtrip/getpid-trace-worksheet.md)

先做工具预检和不启动 QEMU 的静态检查：

```sh
command -v git make qemu-system-riscv64 gdb-multiarch
python3 docs/xv6-tutorial/resources/syscall-roundtrip/run-trace.py --static-only
```

静态检查从 `curriculum.json` 读取 pinned baseline，在临时导出中构建
`user/_usertests` 与 `kernel/kernel`。它必须报告：

- `SYS_getpid=11`，生成 stub 的 `li`、`ecall(0x00000073)`、`ret` 顺序成立；
- `E` 到下一条指令的距离为 4 字节；
- `reparent` 中首次 `jal getpid` 的返回地址等于运行时 stub 中的 `ra`；
- runtime `uservec/userret` 地址由 `TRAMPOLINE` 和链接符号偏移计算，而不是
  把 `0x3ffffff000` 当成未经验证的常量；
- 未知编号 `22` 不在分派表有效范围内。

再运行完整的两个全新实例，并把单一报告写到临时目录：

```sh
TRACE_DIR=$(mktemp -d /tmp/xv6-syscall-trace.XXXXXX)
python3 docs/xv6-tutorial/resources/syscall-roundtrip/run-trace.py \
  --report "$TRACE_DIR/getpid-roundtrip.md"
sed -n '1,240p' "$TRACE_DIR/getpid-roundtrip.md"
```

runner 使用 pinned baseline 的三个临时导出（一次静态、两次动态）、两个私有
`fs.img`、`CPUS=1` 和 loopback GDB 端口。两个动态实例都让 shell 只运行
`usertests reparent`，并只追踪
它第一次进入 `_usertests:getpid` 的往返：

1. 正常实例保持 `a7=11`。
2. 边界实例在 `li` 之后、`ecall` 之前把 `a7` 改为 `22`；除此之外不改
   guest 内存或源码。

GDB 必须按 stub、mutation 前的 `ecall`、`uservec` 入口、现场已保存、
kernel stack、kernel `satp`、`usertrap`、`epc` 已推进但尚未 `intr_on`、
`syscall`、`prepare_return`、runtime `userret`、user `satp`、`sret` 前和
`E+4` 的顺序命中。每个点都读取 QEMU 暴露的 `$priv`；使用 runtime 高地址
是必要条件：ELF 中
`uservec/userret` 的链接地址不是进程运行时的 trampoline 虚拟地址。

在 `uservec` 入口记录原始 `scause/sepc/sstatus`；不要等 `intr_on()` 后再把
这些 CSR 当成原 trap 证据。断点和单步会改变时序，GDB 显示的源码行表示
下一条将执行的语句；报告必须记录这两个限制。

完成后核对报告的 normal/unknown checkpoint 表、精确诊断和 cleanup 段，
再删除临时报告目录：

```sh
rm -r -- "$TRACE_DIR"
git status --short
```

## 有界修改任务

`N/A`。下一单元的[新增最小系统调用](../experiments/add-system-call.md)负责
源码 patch、定向测试和完整回归。本单元只在隔离 guest 的 GDB stop 中临时
改一个 caller-saved 寄存器，退出 QEMU 后状态消失；它不注册新编号、不修改
分派表，也不与下一单元维护两份 patch 流程。

## Oracle、证据、失败路径和局限

- `S`：pinned source、生成 stub 和 ELF 反汇编形成闭环；编号为 `11`，
  `ecall` 编码和 4 字节长度正确；每个报告 checkpoint 都能回到 manifest
  登记的 `path:symbol`，生成文件没有被当作手写 owner。
- `F`：`ra` 把目标限定为 `reparent` 的第一次 `getpid`；实际 `$priv` 在
  stub/返回点为 `0`、内核 checkpoint 为 `1`；`scause=8`、`SPP=0`，user/kernel
  `satp` 与栈在预期 checkpoint 改变；handler 恰好命中一次，动态 pid 经
  `trapframe->a0` 回到用户 `a0`，最终 `pc=E+4`。
- `B`：mutation 前后 `pc/priv/ra/sp/a0/satp` 相同，只有 `a7` 从 `11`
  变为 `22`；handler 命中零次，诊断中的 pid 与当前进程一致，
  `trapframe/user a0=UINT64_MAX`（C `int` 为 `-1`），仍恰好一次到达
  `E+4`。timeout 只是 watchdog，不能代替这些关系断言。
- 资源结果：运行只写临时导出和私有镜像；原 `git status --short` 与共享
  `fs.img` 哈希前后相同，GDB/QEMU、端口和临时树全部清理。
- `C` 不适用：`CPUS=1` 和调试停顿刻意消除并发，本报告不建立跨 hart
  happens-before 或中断时序结论。
- `R` 不适用：没有 crash point、持久化写入或恢复主张；删除私有镜像只是
  cleanup，不是恢复证据。

若 GDB 未命中任一 checkpoint、两个 `satp` 相同、栈没有切换、动态 pid
关系不成立、未知诊断次数不是一、共享输入变化或有进程残留，本单元都不能
晋级。一次 `usertests reparent` 最终通过也不能替代中间状态 oracle。

## 退出产物与后续单元

提交一份填好的 `getpid` 往返报告包；正常与未知编号只是同一报告中的两个
分节。报告必须包含：环境与 pinned baseline、stub/指令检查、完整 checkpoint
表、`E -> E+4` 关系、动态 pid/返回值、未知诊断与 handler 次数、共享状态
哈希、进程/临时目录清理，以及 `full-trampoline-page-table-contract` 和调试
时序限制。任一缺项都不能把本单元标为 `verified`。

本单元拥有的迁移题在[系统调用往返问题](../questions/syscall-roundtrip.md)；
它们与本报告使用同一组源码和运行证据。随后进入
[有界实验：新增最小系统调用](../experiments/add-system-call.md)，把本单元
观察到的声明、编号、stub、分派和 handler 链变成一个可审阅 patch。
