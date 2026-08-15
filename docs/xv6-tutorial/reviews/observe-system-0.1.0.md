# Observe-system 0.1.0 非作者走查记录

- 教程版本：`0.1.0 draft`
- 源码基线：`e6fc75076de152c5446f2c6be9bb80b848e7cc5d`
- 走查时教程提交：`5d9751d48874ebeda180f9d8b65573d55ebc53d7` 加 #4 未提交候选 diff；最终提交保留本记录与修正后的完整 diff
- 走查单元或连续路径：`core.observe-system`
- 匿名入口能力：`OBS-C19-R1`；已满足 Foundation gate 的命令行、C、基础 RISC-V 和 GDB 契约，未参与候选稿编写
- 使用环境：WSL2 Linux x86-64；QEMU 8.2.2、GDB multiarch 15.1；临时独占镜像和单独 GDB 端口

## 观察到的卡点

首轮走查发现两处不能原样放行的问题。正文写“保留四个黑盒”，但正文、manifest 和退出要求实际列出五个；高地址 `userret` 步骤只让学习者搜索 `kernel/memlayout.h:TRAMPOLINE`，没有同时定位 `kernel/riscv.h:MAXVA`，因而不能独立复算 `0x3ffffff000`。

其余路径没有依赖未声明知识。特别是连接 GDB 时 PC 仍在 QEMU ROM、`_entry` 入口处 `sp=0`、源码行尚未执行和多 hart 打印无全序等容易误读的位置，候选稿均给出了明确限制。

## 验收产物

- 源码时间线：`QEMU -> _entry -> start -> main -> userinit -> scheduler -> forkret -> kexec("/init") -> prepare_return -> userret -> sret -> user/ulib.c:start -> init:main -> sh:main -> getcmd -> "$ "`。`user/_init` 的 ELF entry 与 `start` 均为 `0xbc`。
- 三 hart 运行：依次观察到内核启动、`hart 1 starting`、`hart 2 starting`、`init: starting sh`、`$ ` 和精确的 `tutorial-observation` 输出；记录只保留这次顺序，不把 hart 打印顺序当成并发保证。
- 单 hart 运行：同样到达 prompt 并精确输出测试文本，且没有 `hart 1/2 starting`。
- GDB 记录：同一 `CPU#0` 依次命中 `_entry`、`start`、`main`、`userinit`、`scheduler`、`forkret`、`prepare_return` 和运行时高地址 `userret`。对应 PC 为 `0x80000000`、`0x80000054`、`0x80000e1a`、`0x80001b6a`、`0x80001d16`、`0x800018d4`、`0x800023e8`、`0x3ffffff09c`；`forkret` 已在首进程内核栈 `0x3fffffe000` 上。
- 首次用户返回边界：`userret` 处 `a0=0x8000000000087f52`、`sepc=0xbc`、`sstatus=0x200000020`、当前 `satp=0x8000000000087fff`；detach 后同一实例继续出现 `init: starting sh` 和 `$ `。
- 边界与资源：`CPUS=invalid` 在 guest 启动前报告 `smp.cpus` 需要整数并退出 `1`，没有 xv6 启动输出。共享 `fs.img` 前后 SHA-256 均为 `c470503efebc0f8b33d67aed363d65a03a0bd6ca1f1389ccbd276b71d2b60f1a`；启动过的私有镜像发生变化，非法配置镜像不变，证明写入只落在隔离副本。临时目录已删除，无残留 QEMU/GDB。

## 修正与复查

正文把黑盒计数改为五，并把 `first-user-process` 与 `first-user-address-space` 的边界分开。源码追踪新增 `kernel/riscv.h:MAXVA`、可执行的地址计算和 `kernel/proc.c` 中 `prepare_return/trampoline_userret` 调用边；manifest 同步登记这些稳定锚点。复查能从源码得到 `TRAMPOLINE=0x3ffffff000`，随后高地址断点按预期命中。

独立 Spec 审查又指出正文虽然记录了 `userret:sepc=0xbc`，却没有要求学习者把它映射回 `/init` 的 ELF entry。观察任务因此新增 `readelf -h user/_init` 与 `user/init.sym` 检查；复查实际得到 entry `0xbc`、`start=0xbc`，与走查时 `userret` 的 `sepc=0xbc` 相等，首次用户态跳转不再依赖事后断言。

技术复查同时通过普通与 development validator、generated navigation check、5 个 validator 单测、`sh -n` 资源检查和 `git diff --check`。五个显式黑盒保持未解释状态；本轮没有迁移跨越后续 boot/trap owner 的 `docs/questions/boot-and-traps.md`，也没有创建同步副本。

## 结果

| 单元 | 结果 | 晋级依据 |
|---|---|---|
| `core.observe-system` | 通过 | QEMU 到 prompt 的源码/运行时间线、八个 GDB 边界、`S/F/B` 配置实验、镜像隔离和清理证据完整 |

本记录只支持 `core.observe-system` 晋级为 `verified`。它不解除五个黑盒，不提供 `C/R` 结论，也不把启动、echo 或一次多 hart 运行当成 xv6 正确性证明。
