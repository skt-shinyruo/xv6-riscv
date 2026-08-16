# Syscall-roundtrip 0.1.0 非作者走查记录

- 教程版本：`0.1.0 draft`
- 源码基线：`e6fc75076de152c5446f2c6be9bb80b848e7cc5d`
- 走查时教程提交：`8e30c8c407b2ed575984ded89cc5f6e865f5989d` 加 #6 未提交候选 diff；最终提交保留本记录与修正后的完整 diff
- 走查单元或连续路径：`core.syscall-roundtrip`，以及迁移题 `SYSCALL-01/02/03`
- 匿名入口能力：`SYS-C19-R1`；已通过 Foundation gate、`core.observe-system` 与 `core.user-program-and-abi`，未参与候选正文或 runner 编写
- 使用环境：WSL2 Linux x86-64；QEMU 8.2.2、GDB multiarch 15.1、RISC-V GCC 13.3.0、Python 3.12.3；`CPUS=1`、128 MiB、loopback GDB 端口与私有镜像

## 观察到的卡点

初始草稿只有 `syscall`/`sys_getpid` 两个断点，没有可执行的 unknown-number
触发器，也没有记录 stub、runtime trampoline、`satp`/栈切换、`sret` 和
`E+4` 返回点，无法建立 issue 要求的验收 oracle。

首版自动 runner 又有四个实质缺口：把 U/S 模式写成固定文本而没有读取
QEMU 的 `$priv`；缺少 `epc += 4` 后且 `intr_on()` 前的独立 checkpoint；
没有用 `reparent` 中 `jal getpid` 的返回地址限定目标调用；未知路径只显示
修改后的 `a7`，没有证明 mutation 前后其他关键寄存器不变。走查还确认
`usertests` 的 `test reparent: ` 前缀会与内核 unknown 诊断拼在一行，因此
oracle 应精确计数内核诊断片段，不能虚构独占换行。

## 验收产物

- 静态身份：`SYS_getpid=11`；stub=`0x4e2e`，2 字节 `li` 后
  `E=0x4e30` 为 `0x00000073`，`E+4=0x4e34`；`reparent` 的目标调用返回
  地址为 `0x38d6`。runtime `uservec=0x3ffffff000`、
  `userret=0x3ffffff09c`、`sret=0x3ffffff11c`。
- 正常入口：stub 与 `E` 的 `$priv=0`、`ra=0x38d6`、user
  `satp=0x8000000000080025`、`sp=0x11e80`。`uservec` 的 `$priv=1`，
  `scause=8`、`sepc=E`、`SPP=0`，硬件尚未改变 user `satp/sp`。
- 保存与切换：`TRAPFRAME=0x3fffffe000` 保存 user `sp/a0/a7`；随后
  `sp=0x3fffff8000`，kernel `satp=0x8000000000087fff`。`usertrap` 使用
  kernel 页表/栈；`0x800025c8` checkpoint 仍关中断且
  `trapframe->epc=E+4`。
- 正常分派：pid 为 `4`，`trapframe->a7=11`，`sys_getpid` 恰好命中一次；
  handler 结果、`trapframe->a0` 与返回用户态的 `a0` 均为 `4`。
- 正常返回：`userret` 从 kernel `satp/sp` 开始，先换回 user `satp`，再恢复
  user `sp/a7/a0`；`sret` 前 `$priv=1`、`sepc=E+4`，随后 `$priv=0`、
  `pc=E+4`、`ra=0x38d6`。
- 未知编号：第二个全新实例在同一 `E` 只把 `a7: 11 -> 22`；mutation
  前后 `$priv/pc/ra/sp/a0/satp` 相同。`sys_getpid` 命中零次，内核诊断
  `4 usertests: unknown sys call 22` 精确出现一次，trapframe 与用户 `a0`
  均为 `0xffffffffffffffff`，仍只到达一次 `E+4`。
- 时序反例：unknown 路径的 `printk` 在 `intr_on()` 后触发 supervisor
  external interrupt，实时 CSR 一度变为 `sepc=0x80000c20`、
  `scause=0x8000000000000009`，而 `trapframe->epc` 保持 `0x4e34`。
  这实际证明后半程必须信 trapframe，不能在 `syscall` 后把 CSR 当原 trap。
- 问题迁移：原 `BOOT-05/07/08` 已重写为 `SYSCALL-01/02/03` 并配独立答案
  与证据标准；旧位置只保留兼容链接。`BOOT-06/09/10` 仍留给后续 owner，
  没有同步副本。

## 修正与复查

修订后的 GDB 模板实际读取每个 checkpoint 的 `$priv`，增加 mutation 前
`ECALL_BEFORE` 与开中断前 `ADVANCED`，静态解析 `reparent` caller 返回地址，
并比较 mutation 前后的六项未改状态。runner 从 manifest 读取 pinned
baseline，在三次临时导出中完成静态、正常与 unknown 检查，生成一份含两个
分节的报告包。

修订后自动 runner 连续两次通过：`static trace passed`、
`normal dynamic trace passed`、`unknown dynamic trace passed`，最终报告
`getpid round-trip passed: static, normal, unknown, cleanup`。每次运行前后原
工作树状态一致，共享 `fs.img` SHA-256 均为
`c470503efebc0f8b33d67aed363d65a03a0bd6ca1f1389ccbd276b71d2b60f1a`；
runner 自身的临时导出、私有镜像、GDB/QEMU 子进程和端口全部清理。

普通与 development validator、generated navigation check、5 个 validator
单测、Python 编译和 `git diff --check` 均通过。走查确认报告只观察同址
trampoline 与切换结果，没有把 PTE、映射建立、TLB 或跨 hart 安全性从
`full-trampoline-page-table-contract` 中偷渡出来。

## 结果

| 单元 | 结果 | 晋级依据 |
|---|---|---|
| `core.syscall-roundtrip` | 通过 | pinned 静态链、两套隔离 GDB 轨迹、真实特权/页表/栈/trapframe/dispatch/返回状态，以及 unknown + `E+4` 的 `S/F/B` oracle 均闭合 |

本记录只支持 `core.syscall-roundtrip` 晋级为 `verified` 并解除
`syscall-kernel-entry`。它不支持完整 trampoline 页表契约、并发时序或恢复
结论，也不替代下一单元新增系统调用所需的 focused/related/full regressions。
